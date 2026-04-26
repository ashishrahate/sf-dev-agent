"""Unit tests for the metadata index — parsers, SQLite index, orchestrator.

No live org needed. Tests build an in-memory sfdx source tree under tmp_path
and feed it to `ingest_directory`, then exercise the query API.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sf_dev_agent.context import (
    MetadataIndex,
    build_index,
    ingest_directory,
)
from sf_dev_agent.context.parsers.apex_class import ApexClassParser
from sf_dev_agent.context.parsers.apex_trigger import ApexTriggerParser
from sf_dev_agent.context.parsers.custom_object import CustomObjectParser
from sf_dev_agent.context.retriever import RetrieveResult


# ---------------------------------------------------------------------------
# Fixtures: build a small sfdx source tree on disk
# ---------------------------------------------------------------------------

def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def fixture_tree(tmp_path: Path) -> Path:
    """Write a representative sfdx source layout and return its root."""
    base = tmp_path / "force-app" / "main" / "default"

    # ApexClass — a regular handler with sharing, no parent.
    _write(base / "classes" / "AccountHandler.cls",
           "public with sharing class AccountHandler {\n"
           "    public static void handle(List<Account> accounts) {}\n"
           "}\n")

    # ApexClass — extends + implements + a test class.
    _write(base / "classes" / "AccountHandlerExt.cls",
           "public class AccountHandlerExt extends AccountHandler "
           "implements Database.Batchable<sObject>, Schedulable {\n"
           "    public void execute(Database.BatchableContext ctx, "
           "List<sObject> scope) {}\n"
           "}\n")

    _write(base / "classes" / "AccountHandlerTest.cls",
           "@isTest\n"
           "private class AccountHandlerTest {\n"
           "    @isTest static void smoke() { System.assert(true); }\n"
           "}\n")

    # ApexTrigger — references the Account custom object that we ALSO ingest,
    # so the relationship should resolve.
    _write(base / "triggers" / "AccountTrigger.trigger",
           "trigger AccountTrigger on Account (before insert, after update) {\n"
           "    AccountHandler.handle(Trigger.new);\n"
           "}\n")

    # ApexTrigger on a standard object we DON'T ingest (Contact). Relationship
    # should be skipped silently — `target_object` still recorded in metadata.
    _write(base / "triggers" / "ContactTrigger.trigger",
           "trigger ContactTrigger on Contact (before insert) {}\n")

    # CustomObject — Account with two fields under fields/.
    _write(base / "objects" / "Account" / "Account.object-meta.xml",
           "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
           "<CustomObject xmlns=\"http://soap.sforce.com/2006/04/metadata\">\n"
           "  <label>Account</label>\n"
           "  <pluralLabel>Accounts</pluralLabel>\n"
           "  <sharingModel>ReadWrite</sharingModel>\n"
           "  <deploymentStatus>Deployed</deploymentStatus>\n"
           "</CustomObject>\n")

    _write(base / "objects" / "Account" / "fields" / "Region__c.field-meta.xml",
           "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
           "<CustomField xmlns=\"http://soap.sforce.com/2006/04/metadata\">\n"
           "  <fullName>Region__c</fullName>\n"
           "  <label>Region</label>\n"
           "  <type>Picklist</type>\n"
           "  <required>false</required>\n"
           "</CustomField>\n")

    _write(base / "objects" / "Account" / "fields" / "External_Id__c.field-meta.xml",
           "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
           "<CustomField xmlns=\"http://soap.sforce.com/2006/04/metadata\">\n"
           "  <fullName>External_Id__c</fullName>\n"
           "  <label>External Id</label>\n"
           "  <type>Text</type>\n"
           "  <length>50</length>\n"
           "  <unique>true</unique>\n"
           "  <externalId>true</externalId>\n"
           "</CustomField>\n")

    return tmp_path


@pytest.fixture
def built_index(fixture_tree: Path, tmp_path: Path):
    """Run the full orchestrator against the fixture tree and yield the index."""
    db_path = tmp_path / "test_index.db"
    result = ingest_directory(source_dir=fixture_tree, db_path=db_path)
    assert result.success, f"ingest_directory failed: {result}"
    with MetadataIndex(db_path) as index:
        yield index, result


# ---------------------------------------------------------------------------
# Parser unit tests (no DB)
# ---------------------------------------------------------------------------

def test_apex_class_parser_extracts_sharing_and_extends(fixture_tree: Path) -> None:
    parser = ApexClassParser()
    path = fixture_tree / "force-app" / "main" / "default" / "classes" / "AccountHandlerExt.cls"
    result = parser.parse(path)

    assert len(result.components) == 1
    cls = result.components[0]
    assert cls.api_name == "AccountHandlerExt"
    assert cls.metadata["extends"] == "AccountHandler"
    assert "Database.Batchable<sObject>" in cls.metadata["implements"] \
        or "Database.Batchable" in cls.metadata["implements"][0]
    assert "Schedulable" in cls.metadata["implements"]
    # Should emit one EXTENDS relationship; one IMPLEMENTS per interface.
    rel_types = sorted(r.relationship_type for r in result.relationships)
    assert rel_types[0] == "EXTENDS"
    assert "IMPLEMENTS" in rel_types


def test_apex_class_parser_detects_test_class(fixture_tree: Path) -> None:
    parser = ApexClassParser()
    path = fixture_tree / "force-app" / "main" / "default" / "classes" / "AccountHandlerTest.cls"
    result = parser.parse(path)
    assert result.components[0].metadata["is_test"] is True


def test_apex_trigger_parser_extracts_object_and_events(fixture_tree: Path) -> None:
    parser = ApexTriggerParser()
    path = fixture_tree / "force-app" / "main" / "default" / "triggers" / "AccountTrigger.trigger"
    result = parser.parse(path)

    assert len(result.components) == 1
    trigger = result.components[0]
    assert trigger.api_name == "AccountTrigger"
    assert trigger.metadata["target_object"] == "Account"
    assert "before insert" in trigger.metadata["events"]
    assert "after update" in trigger.metadata["events"]

    assert len(result.relationships) == 1
    rel = result.relationships[0]
    assert rel.relationship_type == "TRIGGERS_ON"
    assert rel.target_id == "CustomObject:Account"


def test_custom_object_parser_yields_object_and_fields(fixture_tree: Path) -> None:
    parser = CustomObjectParser()
    path = (
        fixture_tree / "force-app" / "main" / "default"
        / "objects" / "Account" / "Account.object-meta.xml"
    )
    result = parser.parse(path)

    types = sorted(c.component_type for c in result.components)
    assert types == ["CustomField", "CustomField", "CustomObject"]

    object_row = next(c for c in result.components if c.component_type == "CustomObject")
    assert object_row.api_name == "Account"
    assert object_row.metadata["sharingModel"] == "ReadWrite"

    fields = [c for c in result.components if c.component_type == "CustomField"]
    field_names = sorted(f.api_name for f in fields)
    assert field_names == ["External_Id__c", "Region__c"]

    ext_id_field = next(f for f in fields if f.api_name == "External_Id__c")
    assert ext_id_field.parent_id == "CustomObject:Account"
    assert ext_id_field.metadata["external_id"] is True
    assert ext_id_field.metadata["unique"] is True
    assert ext_id_field.metadata["type"] == "Text"

    # Each field emits a FIELD_OF relationship to its parent object.
    field_of_targets = {
        r.target_id for r in result.relationships if r.relationship_type == "FIELD_OF"
    }
    assert field_of_targets == {"CustomObject:Account"}


# ---------------------------------------------------------------------------
# Orchestrator + index integration
# ---------------------------------------------------------------------------

def test_ingest_directory_indexes_expected_components(built_index) -> None:
    index, result = built_index
    assert result.components_indexed >= 6  # 3 classes + 2 triggers + 1 object + 2 fields
    stats = index.stats()
    assert stats.get("ApexClass") == 3
    assert stats.get("ApexTrigger") == 2
    assert stats.get("CustomObject") == 1
    assert stats.get("CustomField") == 2


def test_triggers_on_account_returns_account_trigger(built_index) -> None:
    index, _ = built_index
    triggers = index.triggers_on("Account")
    names = [t.api_name for t in triggers]
    assert names == ["AccountTrigger"]


def test_fields_of_account_returns_both_fields(built_index) -> None:
    index, _ = built_index
    fields = index.fields_of("Account")
    names = sorted(f.api_name for f in fields)
    assert names == ["External_Id__c", "Region__c"]


def test_relationship_to_unindexed_object_is_skipped(built_index) -> None:
    index, result = built_index
    # ContactTrigger references Contact, which we did not ingest. The trigger row
    # exists; the TRIGGERS_ON edge does not.
    contact_trigger = index.find_by_id("ApexTrigger:ContactTrigger")
    assert contact_trigger is not None
    assert contact_trigger.metadata["target_object"] == "Contact"
    # Skipped count includes the dangling Contact edge plus dangling EXTENDS to
    # AccountHandler (which DOES exist in our index, so not skipped) and
    # IMPLEMENTS to Database.Batchable* + Schedulable (which don't, so skipped).
    assert result.relationships_skipped >= 1
    # AccountTrigger -> Account should resolve.
    triggers = index.triggers_on("Account")
    assert any(t.api_name == "AccountTrigger" for t in triggers)


def test_search_finds_by_name_substring(built_index) -> None:
    index, _ = built_index
    hits = index.search("Account")
    names = {h.api_name for h in hits}
    # Account, AccountHandler, AccountHandlerExt, AccountHandlerTest, AccountTrigger
    assert {"Account", "AccountHandler", "AccountTrigger"}.issubset(names)


def test_ingest_is_idempotent(fixture_tree: Path, tmp_path: Path) -> None:
    """Running ingest twice should leave the row count unchanged (upsert, not insert)."""
    db_path = tmp_path / "idempotent.db"
    first = ingest_directory(source_dir=fixture_tree, db_path=db_path)
    second = ingest_directory(source_dir=fixture_tree, db_path=db_path)
    with MetadataIndex(db_path) as index:
        assert index.stats() == {
            "ApexClass": 3, "ApexTrigger": 2, "CustomObject": 1, "CustomField": 2,
        }
    # Both runs report the same component count — no duplicates accumulated.
    assert first.components_indexed == second.components_indexed


# ---------------------------------------------------------------------------
# build_index cleanup behavior (uses a fake retriever — no live org)
# ---------------------------------------------------------------------------

def test_build_index_cleans_up_retrieve_dir_on_success(
    fixture_tree: Path, tmp_path: Path, monkeypatch
) -> None:
    """After a successful retrieve+ingest, the staging dir should be removed."""
    retrieve_dir = tmp_path / "stage"
    db_path = tmp_path / "build.db"

    def fake_retrieve(*, org_alias, component_types, target_dir, timeout=600):
        # Pretend the CLI populated target_dir by copying the fixture tree in.
        import shutil as _shutil
        _shutil.copytree(fixture_tree, target_dir, dirs_exist_ok=True)
        return RetrieveResult(
            success=True,
            output_dir=Path(target_dir),
            component_types=component_types,
            raw={"status": 0},
        )

    monkeypatch.setattr("sf_dev_agent.context.retrieve", fake_retrieve)

    result = build_index(
        org_alias="FakeOrg",
        db_path=db_path,
        retrieve_dir=retrieve_dir,
    )
    assert result.success
    assert result.components_indexed > 0
    assert not retrieve_dir.exists(), "Retrieve dir should be cleaned up after success"


def test_build_index_keeps_retrieve_dir_on_failure(
    tmp_path: Path, monkeypatch
) -> None:
    """If retrieve fails, the staging dir is preserved for debugging."""
    retrieve_dir = tmp_path / "stage"
    db_path = tmp_path / "build.db"
    retrieve_dir.mkdir()
    (retrieve_dir / "marker.txt").write_text("kept", encoding="utf-8")

    def fake_retrieve(*, org_alias, component_types, target_dir, timeout=600):
        return RetrieveResult(
            success=False,
            output_dir=Path(target_dir),
            component_types=component_types,
            raw={},
            error="simulated failure",
        )

    monkeypatch.setattr("sf_dev_agent.context.retrieve", fake_retrieve)

    result = build_index(
        org_alias="FakeOrg",
        db_path=db_path,
        retrieve_dir=retrieve_dir,
    )
    assert not result.success
    assert (retrieve_dir / "marker.txt").exists(), \
        "Retrieve dir should be preserved on failure for debugging"


def test_build_index_respects_cleanup_retrieve_false(
    fixture_tree: Path, tmp_path: Path, monkeypatch
) -> None:
    """cleanup_retrieve=False should keep the staging dir even on success."""
    retrieve_dir = tmp_path / "stage"
    db_path = tmp_path / "build.db"

    def fake_retrieve(*, org_alias, component_types, target_dir, timeout=600):
        import shutil as _shutil
        _shutil.copytree(fixture_tree, target_dir, dirs_exist_ok=True)
        return RetrieveResult(
            success=True,
            output_dir=Path(target_dir),
            component_types=component_types,
            raw={"status": 0},
        )

    monkeypatch.setattr("sf_dev_agent.context.retrieve", fake_retrieve)

    result = build_index(
        org_alias="FakeOrg",
        db_path=db_path,
        retrieve_dir=retrieve_dir,
        cleanup_retrieve=False,
    )
    assert result.success
    assert retrieve_dir.exists(), "Retrieve dir should be preserved when cleanup is opted out"
