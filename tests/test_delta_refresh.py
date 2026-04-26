"""Unit tests for the delta-refresh planner and orchestration.

The Tooling-API fetcher and `sf project retrieve start` are both stubbed
via monkeypatch — these tests drive the full delta path deterministically
without touching a live org.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from sf_dev_agent.context import (
    DeltaPlan,
    MetadataIndex,
    OrgComponent,
    OrgInventory,
    build_index,
    compute_deltas,
    ingest_directory,
)
from sf_dev_agent.context.retriever import RetrieveResult


# ---------------------------------------------------------------------------
# Fixture sources we'll feed to the stubbed retriever
# ---------------------------------------------------------------------------

def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def fixture_v1(tmp_path: Path) -> Path:
    """Initial 'org snapshot': two ApexClasses, one ApexTrigger."""
    base = tmp_path / "v1" / "force-app" / "main" / "default"
    _write(base / "classes" / "AccountHandler.cls",
           "public with sharing class AccountHandler {}\n")
    _write(base / "classes" / "RecordSelectorController.cls",
           "public class RecordSelectorController {}\n")
    _write(base / "triggers" / "AccountTrigger.trigger",
           "trigger AccountTrigger on Account (before insert) {}\n")
    return tmp_path / "v1"


@pytest.fixture
def fixture_v2_delta(tmp_path: Path) -> Path:
    """Just the components a targeted retrieve would actually return for the
    v1->v2 transition: the edited AccountHandler and the new ContactHandler.
    AccountTrigger is unchanged, so it would NOT be in the staging dir; and
    RecordSelectorController is gone, so it's nowhere either."""
    base = tmp_path / "v2_delta" / "force-app" / "main" / "default"
    _write(base / "classes" / "AccountHandler.cls",
           "public with sharing class AccountHandler {\n"
           "    // edited body\n"
           "    public static void changed() {}\n"
           "}\n")
    _write(base / "classes" / "ContactHandler.cls",
           "public class ContactHandler {}\n")
    return tmp_path / "v2_delta"


# ---------------------------------------------------------------------------
# compute_deltas — pure planner tests
# ---------------------------------------------------------------------------

def test_compute_deltas_first_run_marks_everything_to_fetch() -> None:
    inv = OrgInventory(
        types_queried=["ApexClass"],
        components=[
            OrgComponent("ApexClass", "Foo", "2026-04-01T10:00:00.000+0000"),
            OrgComponent("ApexClass", "Bar", "2026-04-01T11:00:00.000+0000"),
        ],
    )
    plan = compute_deltas(inv, indexed={}, requested_types=["ApexClass"])
    assert sorted(plan.to_fetch) == ["ApexClass:Bar", "ApexClass:Foo"]
    assert plan.to_delete == []
    assert plan.unchanged == []


def test_compute_deltas_unchanged_skipped() -> None:
    inv = OrgInventory(
        types_queried=["ApexClass"],
        components=[
            OrgComponent("ApexClass", "Foo", "2026-04-01T10:00:00.000+0000"),
        ],
    )
    plan = compute_deltas(
        inv,
        indexed={"ApexClass:Foo": "2026-04-02T00:00:00+00:00"},  # we indexed AFTER it changed
        requested_types=["ApexClass"],
    )
    assert plan.to_fetch == []
    assert plan.unchanged == ["ApexClass:Foo"]


def test_compute_deltas_modified_org_component_re_fetches() -> None:
    inv = OrgInventory(
        types_queried=["ApexClass"],
        components=[
            OrgComponent("ApexClass", "Foo", "2026-04-05T10:00:00.000+0000"),
        ],
    )
    plan = compute_deltas(
        inv,
        indexed={"ApexClass:Foo": "2026-04-01T00:00:00+00:00"},  # org changed since we indexed
        requested_types=["ApexClass"],
    )
    assert plan.to_fetch == ["ApexClass:Foo"]


def test_compute_deltas_deletion() -> None:
    """A class in the index but not in the org's inventory should be deleted."""
    inv = OrgInventory(
        types_queried=["ApexClass"],
        components=[
            OrgComponent("ApexClass", "Foo", "2026-04-01T10:00:00.000+0000"),
        ],
    )
    plan = compute_deltas(
        inv,
        indexed={
            "ApexClass:Foo": "2026-04-02T00:00:00+00:00",
            "ApexClass:Removed": "2026-04-02T00:00:00+00:00",
        },
        requested_types=["ApexClass"],
    )
    assert plan.to_delete == ["ApexClass:Removed"]
    assert plan.unchanged == ["ApexClass:Foo"]


def test_compute_deltas_does_not_delete_untyped_inventory() -> None:
    """If we never queried a type, rows of that type must NOT be flagged for deletion."""
    inv = OrgInventory(
        types_queried=["ApexClass"],
        components=[],
    )
    plan = compute_deltas(
        inv,
        indexed={
            "ApexTrigger:KeepMe": "2026-04-02T00:00:00+00:00",  # type not queried
        },
        requested_types=["ApexClass"],
    )
    assert plan.to_delete == [], \
        "Rows of types we didn't inventory must be left alone"


def test_compute_deltas_unsupported_types_surfaced() -> None:
    inv = OrgInventory(types_queried=["ApexClass"], components=[])
    plan = compute_deltas(
        inv,
        indexed={},
        requested_types=["ApexClass", "Flow"],
    )
    assert "Flow" in plan.unsupported_types
    assert "ApexClass" not in plan.unsupported_types
    assert "CustomObject" not in plan.unsupported_types  # now delta-supported


# ---------------------------------------------------------------------------
# build_index delta path — orchestration with stubbed CLI
# ---------------------------------------------------------------------------

@pytest.fixture
def stub_inventory_and_retrieve(monkeypatch, tmp_path: Path):
    """Helper that returns setters for inventory + retrieve behavior."""
    state: dict = {
        "inventory": OrgInventory(),
        "retrieve_source": None,           # path to copy into retrieve target_dir
        "components_retrieve_source": None,  # path to copy on targeted retrieve
        "calls": {"retrieve": 0, "retrieve_components": 0, "fetch_inventory": 0},
    }

    def fake_fetch_inventory(org_alias, component_types, timeout=120):
        state["calls"]["fetch_inventory"] += 1
        return state["inventory"]

    def fake_retrieve(*, org_alias, component_types, target_dir, timeout=600):
        state["calls"]["retrieve"] += 1
        target_dir = Path(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        if state["retrieve_source"]:
            shutil.copytree(state["retrieve_source"], target_dir, dirs_exist_ok=True)
        return RetrieveResult(
            success=True,
            output_dir=target_dir,
            component_types=list(component_types),
            raw={"status": 0},
        )

    def fake_retrieve_components(*, org_alias, component_ids, target_dir, timeout=600):
        state["calls"]["retrieve_components"] += 1
        target_dir = Path(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        if state["components_retrieve_source"]:
            shutil.copytree(
                state["components_retrieve_source"], target_dir, dirs_exist_ok=True,
            )
        types = sorted({cid.split(":", 1)[0] for cid in component_ids})
        return RetrieveResult(
            success=True,
            output_dir=target_dir,
            component_types=types,
            raw={"status": 0, "requested": list(component_ids)},
        )

    monkeypatch.setattr("sf_dev_agent.context.fetch_org_inventory", fake_fetch_inventory)
    monkeypatch.setattr("sf_dev_agent.context.retrieve", fake_retrieve)
    monkeypatch.setattr("sf_dev_agent.context.retrieve_components", fake_retrieve_components)

    return state


def test_build_index_delta_first_run_fetches_all(
    stub_inventory_and_retrieve, fixture_v1: Path, tmp_path: Path
) -> None:
    state = stub_inventory_and_retrieve
    state["inventory"] = OrgInventory(
        types_queried=["ApexClass", "ApexTrigger"],
        components=[
            OrgComponent("ApexClass", "AccountHandler", "2026-04-01T10:00:00.000+0000"),
            OrgComponent("ApexClass", "RecordSelectorController", "2026-04-01T11:00:00.000+0000"),
            OrgComponent("ApexTrigger", "AccountTrigger", "2026-04-01T12:00:00.000+0000"),
        ],
    )
    state["components_retrieve_source"] = fixture_v1

    db_path = tmp_path / "delta1.db"
    result = build_index(
        org_alias="StubOrg",
        db_path=db_path,
        retrieve_dir=tmp_path / "stage",
        component_types=["ApexClass", "ApexTrigger"],
        delta=True,
    )

    assert result.success
    assert result.delta_mode is True
    assert result.components_unchanged == 0
    assert result.components_deleted == 0
    # First run: 3 components fetched (2 classes + 1 trigger).
    assert result.components_fetched == 3
    assert state["calls"]["retrieve_components"] == 1, \
        "Targeted retrieve should fire once with all 3 component ids"


def test_build_index_delta_no_changes_is_a_noop(
    stub_inventory_and_retrieve, fixture_v1: Path, tmp_path: Path
) -> None:
    state = stub_inventory_and_retrieve
    db_path = tmp_path / "delta2.db"

    # Seed: ingest the v1 tree so the index already knows about all three components.
    ingest_directory(source_dir=fixture_v1, db_path=db_path)

    # The org's "current" inventory matches what we already have, with timestamps
    # OLDER than our last_indexed_at (i.e. nothing has changed since indexing).
    state["inventory"] = OrgInventory(
        types_queried=["ApexClass", "ApexTrigger"],
        components=[
            OrgComponent("ApexClass", "AccountHandler", "2020-01-01T00:00:00.000+0000"),
            OrgComponent("ApexClass", "RecordSelectorController", "2020-01-01T00:00:00.000+0000"),
            OrgComponent("ApexTrigger", "AccountTrigger", "2020-01-01T00:00:00.000+0000"),
        ],
    )

    result = build_index(
        org_alias="StubOrg",
        db_path=db_path,
        retrieve_dir=tmp_path / "stage",
        component_types=["ApexClass", "ApexTrigger"],
        delta=True,
    )

    assert result.success
    assert result.delta_mode is True
    assert result.components_unchanged == 3
    assert result.components_fetched == 0
    assert result.components_deleted == 0
    assert state["calls"]["retrieve_components"] == 0, \
        "Targeted retrieve should NOT fire when nothing changed"


def test_build_index_delta_handles_modify_and_delete(
    stub_inventory_and_retrieve, fixture_v1: Path, fixture_v2_delta: Path, tmp_path: Path
) -> None:
    """v1 -> v2: AccountHandler edited; ContactHandler new; RecordSelectorController deleted."""
    state = stub_inventory_and_retrieve
    db_path = tmp_path / "delta3.db"

    # Seed with v1 so all three v1 components are in the index.
    ingest_directory(source_dir=fixture_v1, db_path=db_path)

    # Inventory reflects v2 timestamps: AccountHandler newer than indexed,
    # ContactHandler is new (not indexed), RecordSelectorController gone,
    # AccountTrigger unchanged.
    state["inventory"] = OrgInventory(
        types_queried=["ApexClass", "ApexTrigger"],
        components=[
            OrgComponent("ApexClass", "AccountHandler", "2099-01-01T00:00:00.000+0000"),  # newer
            OrgComponent("ApexClass", "ContactHandler", "2099-01-01T00:00:00.000+0000"),  # new
            OrgComponent("ApexTrigger", "AccountTrigger", "2020-01-01T00:00:00.000+0000"),  # unchanged
        ],
    )
    # Targeted retrieve drops only the modified + new components.
    # RecordSelectorController is absent (deleted from org); AccountTrigger is
    # absent (unchanged, so no re-fetch).
    state["components_retrieve_source"] = fixture_v2_delta

    result = build_index(
        org_alias="StubOrg",
        db_path=db_path,
        retrieve_dir=tmp_path / "stage",
        component_types=["ApexClass", "ApexTrigger"],
        delta=True,
    )

    assert result.success
    assert result.delta_mode is True
    # AccountHandler (modified) + ContactHandler (new) -> 2 fetched
    assert result.components_fetched == 2
    # RecordSelectorController absent from inventory -> deleted
    assert result.components_deleted == 1
    # AccountTrigger unchanged
    assert result.components_unchanged == 1

    # Index should now hold AccountHandler, ContactHandler, AccountTrigger,
    # but NOT RecordSelectorController.
    with MetadataIndex(db_path) as index:
        ids = {c.id for c in index.find_by_type("ApexClass")}
        ids.update({c.id for c in index.find_by_type("ApexTrigger")})
    assert "ApexClass:RecordSelectorController" not in ids
    assert "ApexClass:AccountHandler" in ids
    assert "ApexClass:ContactHandler" in ids
    assert "ApexTrigger:AccountTrigger" in ids


def test_build_index_full_bypasses_delta(
    stub_inventory_and_retrieve, fixture_v1: Path, tmp_path: Path
) -> None:
    """delta=False forces the full-retrieve path even when an index exists."""
    state = stub_inventory_and_retrieve
    state["retrieve_source"] = fixture_v1
    db_path = tmp_path / "delta4.db"

    result = build_index(
        org_alias="StubOrg",
        db_path=db_path,
        retrieve_dir=tmp_path / "stage",
        component_types=["ApexClass", "ApexTrigger"],
        delta=False,
    )

    assert result.success
    assert result.delta_mode is False
    assert state["calls"]["fetch_inventory"] == 0, "Full mode shouldn't query inventory"
    assert state["calls"]["retrieve"] == 1, "Full mode should call the bulk retrieve"
    assert state["calls"]["retrieve_components"] == 0


# ---------------------------------------------------------------------------
# CustomObject inventory — two-query merge
# ---------------------------------------------------------------------------

def test_fetch_org_inventory_custom_object_merges_field_max(monkeypatch) -> None:
    """CustomObject timestamp should be max(object_lmd, max_field_lmd)."""
    from sf_dev_agent.context import delta as delta_module

    # Two queries fire: object metadata, then field aggregate.
    # Match on the SOQL prefix so we can return the right canned payload.
    def fake_query(org_alias, query, timeout):
        if "FROM CustomObject" in query:
            return [
                {
                    "Id": "01I0000000ObjA",
                    "DeveloperName": "Project_Task",
                    "NamespacePrefix": None,
                    "LastModifiedDate": "2026-03-01T10:00:00.000+0000",
                },
                {
                    "Id": "01I0000000ObjB",
                    "DeveloperName": "Invoice",
                    "NamespacePrefix": "acme",
                    "LastModifiedDate": "2026-04-01T10:00:00.000+0000",
                },
            ], None
        if "FROM CustomField" in query:
            return [
                # ObjA's newest field is *newer* than the object's own LMD.
                {"TableEnumOrId": "01I0000000ObjA", "lmd": "2026-04-15T08:00:00.000+0000"},
                # ObjB's newest field is *older* than the object's LMD.
                {"TableEnumOrId": "01I0000000ObjB", "lmd": "2025-01-01T00:00:00.000+0000"},
            ], None
        return [], None

    monkeypatch.setattr(delta_module, "_query_tooling", fake_query)

    inv = delta_module.fetch_org_inventory("StubOrg", ["CustomObject"])

    assert inv.errors == []
    by_name = {c.api_name: c for c in inv.components}
    # Project_Task__c picks the field's newer timestamp.
    assert "Project_Task__c" in by_name
    assert by_name["Project_Task__c"].last_modified_at == "2026-04-15T08:00:00.000+0000"
    # acme__Invoice__c picks the object's newer timestamp.
    assert "acme__Invoice__c" in by_name
    assert by_name["acme__Invoice__c"].last_modified_at == "2026-04-01T10:00:00.000+0000"


def test_fetch_org_inventory_custom_object_field_query_failure_falls_back(
    monkeypatch,
) -> None:
    """If the CustomField aggregate fails, fall back to object-only timestamps."""
    from sf_dev_agent.context import delta as delta_module

    def fake_query(org_alias, query, timeout):
        if "FROM CustomObject" in query:
            return [
                {
                    "Id": "01I0000000Obj1",
                    "DeveloperName": "Widget",
                    "NamespacePrefix": None,
                    "LastModifiedDate": "2026-03-01T10:00:00.000+0000",
                },
            ], None
        if "FROM CustomField" in query:
            return [], "permissions: insufficient access"
        return [], None

    monkeypatch.setattr(delta_module, "_query_tooling", fake_query)

    inv = delta_module.fetch_org_inventory("StubOrg", ["CustomObject"])

    # Error captured, but inventory still returns the object.
    assert any("CustomField" in e for e in inv.errors)
    assert len(inv.components) == 1
    assert inv.components[0].api_name == "Widget__c"
    assert inv.components[0].last_modified_at == "2026-03-01T10:00:00.000+0000"


def test_compute_deltas_custom_object_modified_via_field() -> None:
    """A field-only edit (reflected in the merged timestamp) triggers re-fetch."""
    inv = OrgInventory(
        types_queried=["CustomObject"],
        components=[
            # Merged timestamp newer than what we indexed.
            OrgComponent("CustomObject", "Project_Task__c", "2026-04-15T08:00:00.000+0000"),
        ],
    )
    plan = compute_deltas(
        inv,
        indexed={"CustomObject:Project_Task__c": "2026-03-15T00:00:00+00:00"},
        requested_types=["CustomObject"],
    )
    assert plan.to_fetch == ["CustomObject:Project_Task__c"]


def test_compute_deltas_custom_object_deletion_marks_object_only() -> None:
    """When a CustomObject is gone from the org, only the parent goes in to_delete.

    Child CustomFields cascade automatically via the DB FK on parent_id —
    the planner doesn't need to enumerate them.
    """
    inv = OrgInventory(
        types_queried=["CustomObject"],
        components=[],
    )
    plan = compute_deltas(
        inv,
        indexed={
            "CustomObject:Removed__c": "2026-03-01T00:00:00+00:00",
            # CustomField rows of the same parent — must NOT show up in to_delete
            # because their type wasn't directly queried.
            "CustomField:Removed__c.Note__c": "2026-03-01T00:00:00+00:00",
        },
        requested_types=["CustomObject"],
    )
    assert plan.to_delete == ["CustomObject:Removed__c"]


# ---------------------------------------------------------------------------
# CustomObject orchestration — child cleanup on re-fetch
# ---------------------------------------------------------------------------

@pytest.fixture
def fixture_object_v1(tmp_path: Path) -> Path:
    """A CustomObject with two fields."""
    base = tmp_path / "obj_v1" / "force-app" / "main" / "default" / "objects" / "Project_Task__c"
    _write(
        base / "Project_Task__c.object-meta.xml",
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<CustomObject xmlns="http://soap.sforce.com/2006/04/metadata">\n'
        '  <label>Project Task</label>\n'
        '  <sharingModel>ReadWrite</sharingModel>\n'
        '</CustomObject>\n',
    )
    _write(
        base / "fields" / "Note__c.field-meta.xml",
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<CustomField xmlns="http://soap.sforce.com/2006/04/metadata">\n'
        '  <fullName>Note__c</fullName>\n'
        '  <label>Note</label>\n'
        '  <type>Text</type>\n'
        '  <length>255</length>\n'
        '</CustomField>\n',
    )
    _write(
        base / "fields" / "DueDate__c.field-meta.xml",
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<CustomField xmlns="http://soap.sforce.com/2006/04/metadata">\n'
        '  <fullName>DueDate__c</fullName>\n'
        '  <label>Due Date</label>\n'
        '  <type>Date</type>\n'
        '</CustomField>\n',
    )
    return tmp_path / "obj_v1"


@pytest.fixture
def fixture_object_v2_field_dropped(tmp_path: Path) -> Path:
    """Same CustomObject but with DueDate__c removed (only Note__c remains)."""
    base = tmp_path / "obj_v2" / "force-app" / "main" / "default" / "objects" / "Project_Task__c"
    _write(
        base / "Project_Task__c.object-meta.xml",
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<CustomObject xmlns="http://soap.sforce.com/2006/04/metadata">\n'
        '  <label>Project Task</label>\n'
        '  <sharingModel>ReadWrite</sharingModel>\n'
        '</CustomObject>\n',
    )
    _write(
        base / "fields" / "Note__c.field-meta.xml",
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<CustomField xmlns="http://soap.sforce.com/2006/04/metadata">\n'
        '  <fullName>Note__c</fullName>\n'
        '  <label>Note (renamed)</label>\n'
        '  <type>Text</type>\n'
        '  <length>500</length>\n'
        '</CustomField>\n',
    )
    return tmp_path / "obj_v2"


def test_build_index_delta_custom_object_drops_stale_child_field(
    stub_inventory_and_retrieve,
    fixture_object_v1: Path,
    fixture_object_v2_field_dropped: Path,
    tmp_path: Path,
) -> None:
    """When a CustomObject is re-fetched, fields that disappeared in the org get pruned."""
    state = stub_inventory_and_retrieve
    db_path = tmp_path / "obj_delta.db"

    # Seed: ingest v1 so Project_Task__c has both Note__c + DueDate__c.
    ingest_directory(source_dir=fixture_object_v1, db_path=db_path)
    with MetadataIndex(db_path) as index:
        v1_field_ids = {c.id for c in index.fields_of("Project_Task__c")}
    assert "CustomField:Project_Task__c.Note__c" in v1_field_ids
    assert "CustomField:Project_Task__c.DueDate__c" in v1_field_ids

    # Org inventory: object's merged timestamp moved past indexed -> re-fetch.
    state["inventory"] = OrgInventory(
        types_queried=["CustomObject"],
        components=[
            OrgComponent("CustomObject", "Project_Task__c", "2099-01-01T00:00:00.000+0000"),
        ],
    )
    # Targeted retrieve returns the v2 source (DueDate__c gone).
    state["components_retrieve_source"] = fixture_object_v2_field_dropped

    result = build_index(
        org_alias="StubOrg",
        db_path=db_path,
        retrieve_dir=tmp_path / "stage_obj",
        component_types=["CustomObject"],
        delta=True,
    )

    assert result.success
    assert result.delta_mode is True
    # 1 parent re-fetched + 1 surviving child re-ingested = 2 components written.
    # (DueDate__c was wiped before re-ingest; Note__c is upserted fresh.)
    assert result.components_fetched >= 2

    with MetadataIndex(db_path) as index:
        post_field_ids = {c.id for c in index.fields_of("Project_Task__c")}
    assert "CustomField:Project_Task__c.Note__c" in post_field_ids, \
        "Surviving field should still be in the index"
    assert "CustomField:Project_Task__c.DueDate__c" not in post_field_ids, \
        "Field deleted from the org should be pruned from the index"


def test_build_index_delta_custom_object_full_delete_cascades_fields(
    stub_inventory_and_retrieve,
    fixture_object_v1: Path,
    tmp_path: Path,
) -> None:
    """Deleting the whole CustomObject should cascade-drop its child fields via FK."""
    state = stub_inventory_and_retrieve
    db_path = tmp_path / "obj_cascade.db"

    # Seed v1.
    ingest_directory(source_dir=fixture_object_v1, db_path=db_path)
    with MetadataIndex(db_path) as index:
        assert index.find_by_id("CustomObject:Project_Task__c") is not None
        assert len(index.fields_of("Project_Task__c")) == 2

    # Org inventory is empty — the object is gone.
    state["inventory"] = OrgInventory(
        types_queried=["CustomObject"],
        components=[],
    )
    # No targeted retrieve will fire (nothing to fetch).
    base = tmp_path / "src_empty" / "force-app" / "main" / "default"
    base.mkdir(parents=True, exist_ok=True)
    state["components_retrieve_source"] = tmp_path / "src_empty"

    result = build_index(
        org_alias="StubOrg",
        db_path=db_path,
        retrieve_dir=tmp_path / "stage_cascade",
        component_types=["CustomObject"],
        delta=True,
    )

    assert result.success
    assert result.components_deleted == 1, "Only the parent is in to_delete"

    with MetadataIndex(db_path) as index:
        # FK CASCADE should have removed both child fields when the parent was deleted.
        assert index.find_by_id("CustomObject:Project_Task__c") is None
        assert index.fields_of("Project_Task__c") == []


def test_build_index_delta_supports_mixed_types(
    stub_inventory_and_retrieve, tmp_path: Path
) -> None:
    """Delta-supported types take the delta path; unsupported types take the full path.

    Flow stands in for any not-yet-supported metadata type — the orchestrator
    has to gracefully accept the mix and route each side to the right path.
    """
    state = stub_inventory_and_retrieve

    # Only ApexClass appears in inventory (Flow isn't in SUPPORTED_DELTA_TYPES).
    state["inventory"] = OrgInventory(
        types_queried=["ApexClass"],
        components=[
            OrgComponent("ApexClass", "Foo", "2026-04-01T00:00:00.000+0000"),
        ],
    )
    # Both retrieve paths land their (empty) source in the same staging dir.
    base = tmp_path / "src" / "force-app" / "main" / "default"
    base.mkdir(parents=True, exist_ok=True)
    state["retrieve_source"] = tmp_path / "src"
    state["components_retrieve_source"] = tmp_path / "src"

    db_path = tmp_path / "mixed.db"
    result = build_index(
        org_alias="StubOrg",
        db_path=db_path,
        retrieve_dir=tmp_path / "stage",
        component_types=["ApexClass", "Flow"],
        delta=True,
    )

    assert result.success
    assert result.delta_mode is True
    # Inventory queried once for ApexClass only.
    assert state["calls"]["fetch_inventory"] == 1
    # Flow took the full path.
    assert state["calls"]["retrieve"] == 1
    # ApexClass took the delta path.
    assert state["calls"]["retrieve_components"] == 1
