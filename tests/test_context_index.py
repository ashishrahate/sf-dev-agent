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
from sf_dev_agent.context.parsers.flow import FlowParser
from sf_dev_agent.context.parsers.lwc import LWCParser
from sf_dev_agent.context.parsers.record_type import RecordTypeParser
from sf_dev_agent.context.parsers.validation_rule import ValidationRuleParser
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
    # AND calls into AccountHandler (also indexed) so a REFERENCES edge should
    # resolve. The standard `Trigger.new` reference must NOT become an edge
    # (Trigger is filtered as an Apex built-in).
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

    # ValidationRule on Account — sibling of fields/.
    _write(base / "objects" / "Account" / "validationRules" / "Region_Required.validationRule-meta.xml",
           "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
           "<ValidationRule xmlns=\"http://soap.sforce.com/2006/04/metadata\">\n"
           "  <fullName>Region_Required</fullName>\n"
           "  <active>true</active>\n"
           "  <description>Region must be set on all accounts.</description>\n"
           "  <errorConditionFormula>ISBLANK(Region__c)</errorConditionFormula>\n"
           "  <errorDisplayField>Region__c</errorDisplayField>\n"
           "  <errorMessage>Please specify a region.</errorMessage>\n"
           "</ValidationRule>\n")

    # RecordType on Account — sibling of validationRules/.
    _write(base / "objects" / "Account" / "recordTypes" / "Customer.recordType-meta.xml",
           "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
           "<RecordType xmlns=\"http://soap.sforce.com/2006/04/metadata\">\n"
           "  <fullName>Customer</fullName>\n"
           "  <active>true</active>\n"
           "  <label>Customer</label>\n"
           "  <description>External customer accounts.</description>\n"
           "</RecordType>\n")

    # LWC bundle — imports an Apex method and a schema field.
    _write(base / "lwc" / "accountSummary" / "accountSummary.js-meta.xml",
           "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
           "<LightningComponentBundle xmlns=\"http://soap.sforce.com/2006/04/metadata\">\n"
           "  <apiVersion>62.0</apiVersion>\n"
           "  <isExposed>true</isExposed>\n"
           "  <masterLabel>Account Summary</masterLabel>\n"
           "  <description>Renders an account summary panel.</description>\n"
           "  <targets>\n"
           "    <target>lightning__RecordPage</target>\n"
           "    <target>lightning__AppPage</target>\n"
           "  </targets>\n"
           "</LightningComponentBundle>\n")
    _write(base / "lwc" / "accountSummary" / "accountSummary.js",
           "import { LightningElement, wire } from 'lwc';\n"
           "import getAccount from '@salesforce/apex/AccountHandler.getAccount';\n"
           "import REGION_FIELD from '@salesforce/schema/Account.Region__c';\n"
           "export default class AccountSummary extends LightningElement {\n"
           "  @wire(getAccount) account;\n"
           "}\n")
    _write(base / "lwc" / "accountSummary" / "accountSummary.html",
           "<template>\n  <p>Account summary</p>\n</template>\n")

    # Flow — record-triggered, calls an Apex invocable, updates Contact.
    _write(base / "flows" / "Update_Account_Region.flow-meta.xml",
           "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
           "<Flow xmlns=\"http://soap.sforce.com/2006/04/metadata\">\n"
           "  <label>Update Account Region</label>\n"
           "  <processType>RecordTriggerFlow</processType>\n"
           "  <status>Active</status>\n"
           "  <interviewLabel>Update Account Region</interviewLabel>\n"
           "  <start>\n"
           "    <object>Account</object>\n"
           "    <recordTriggerType>Update</recordTriggerType>\n"
           "    <triggerType>RecordAfterSave</triggerType>\n"
           "  </start>\n"
           "  <actionCalls>\n"
           "    <name>Call_Apex</name>\n"
           "    <actionName>AccountHandler</actionName>\n"
           "    <actionType>apex</actionType>\n"
           "  </actionCalls>\n"
           "  <recordUpdates>\n"
           "    <name>Touch_Contact</name>\n"
           "    <object>Contact</object>\n"
           "  </recordUpdates>\n"
           "</Flow>\n")

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

    triggers_on = [r for r in result.relationships if r.relationship_type == "TRIGGERS_ON"]
    assert len(triggers_on) == 1
    assert triggers_on[0].target_id == "CustomObject:Account"

    # AccountHandler.handle(...) inside the trigger should produce a REFERENCES edge.
    references = [r for r in result.relationships if r.relationship_type == "REFERENCES"]
    targets = {r.target_id for r in references}
    assert "ApexClass:AccountHandler" in targets


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


def test_validation_rule_parser_extracts_attrs_and_emits_validates_on(
    fixture_tree: Path,
) -> None:
    parser = ValidationRuleParser()
    path = (
        fixture_tree / "force-app" / "main" / "default"
        / "objects" / "Account" / "validationRules"
        / "Region_Required.validationRule-meta.xml"
    )
    result = parser.parse(path)

    assert len(result.components) == 1
    rule = result.components[0]
    assert rule.component_type == "ValidationRule"
    assert rule.api_name == "Region_Required"
    assert rule.id == "ValidationRule:Account.Region_Required"
    # Top-level component — no parent_id, owning object is in metadata.
    assert rule.parent_id is None
    assert rule.metadata["object"] == "Account"
    assert rule.metadata["active"] is True
    assert rule.metadata["error_condition_formula"] == "ISBLANK(Region__c)"
    assert rule.metadata["error_message"] == "Please specify a region."
    assert rule.metadata["error_display_field"] == "Region__c"

    validates_on = [
        r for r in result.relationships if r.relationship_type == "VALIDATES_ON"
    ]
    assert len(validates_on) == 1
    assert validates_on[0].source_id == rule.id
    assert validates_on[0].target_id == "CustomObject:Account"


def test_record_type_parser_extracts_attrs_and_emits_record_type_of(
    fixture_tree: Path,
) -> None:
    parser = RecordTypeParser()
    path = (
        fixture_tree / "force-app" / "main" / "default"
        / "objects" / "Account" / "recordTypes"
        / "Customer.recordType-meta.xml"
    )
    result = parser.parse(path)

    assert len(result.components) == 1
    rec = result.components[0]
    assert rec.component_type == "RecordType"
    assert rec.api_name == "Customer"
    assert rec.id == "RecordType:Account.Customer"
    assert rec.parent_id is None
    assert rec.metadata["object"] == "Account"
    assert rec.metadata["active"] is True
    assert rec.metadata["label"] == "Customer"

    rt_of = [
        r for r in result.relationships if r.relationship_type == "RECORD_TYPE_OF"
    ]
    assert len(rt_of) == 1
    assert rt_of[0].source_id == rec.id
    assert rt_of[0].target_id == "CustomObject:Account"


def test_flow_parser_extracts_trigger_apex_and_record_objects(
    fixture_tree: Path,
) -> None:
    parser = FlowParser()
    path = (
        fixture_tree / "force-app" / "main" / "default"
        / "flows" / "Update_Account_Region.flow-meta.xml"
    )
    result = parser.parse(path)

    assert len(result.components) == 1
    flow = result.components[0]
    assert flow.component_type == "Flow"
    assert flow.api_name == "Update_Account_Region"
    assert flow.id == "Flow:Update_Account_Region"
    assert flow.metadata["process_type"] == "RecordTriggerFlow"
    assert flow.metadata["status"] == "Active"
    assert flow.metadata["start_object"] == "Account"
    assert flow.metadata["record_trigger_type"] == "Update"
    assert flow.metadata["apex_action_classes"] == ["AccountHandler"]
    # Contact is touched via recordUpdates; Account is the start_object so it
    # is captured as TRIGGERS_ON, not duplicated as REFERENCES_OBJECT.
    assert flow.metadata["record_objects"] == ["Contact"]

    by_type: dict[str, list] = {}
    for r in result.relationships:
        by_type.setdefault(r.relationship_type, []).append(r)

    assert len(by_type["TRIGGERS_ON"]) == 1
    assert by_type["TRIGGERS_ON"][0].target_id == "CustomObject:Account"
    assert len(by_type["REFERENCES"]) == 1
    assert by_type["REFERENCES"][0].target_id == "ApexClass:AccountHandler"
    assert len(by_type["REFERENCES_OBJECT"]) == 1
    assert by_type["REFERENCES_OBJECT"][0].target_id == "CustomObject:Contact"


def test_flow_parser_handles_autolaunched_with_no_start_object(
    tmp_path: Path,
) -> None:
    """Autolaunched flows have no start.object — should still parse cleanly,
    with no TRIGGERS_ON edge but Apex actions still surfaced."""
    flow_path = tmp_path / "Reusable_Helper.flow-meta.xml"
    flow_path.write_text(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<Flow xmlns=\"http://soap.sforce.com/2006/04/metadata\">\n"
        "  <label>Reusable Helper</label>\n"
        "  <processType>AutoLaunchedFlow</processType>\n"
        "  <status>Active</status>\n"
        "  <actionCalls>\n"
        "    <name>Util</name>\n"
        "    <actionName>UtilService</actionName>\n"
        "    <actionType>apex</actionType>\n"
        "  </actionCalls>\n"
        "</Flow>\n",
        encoding="utf-8",
    )
    result = FlowParser().parse(flow_path)
    assert len(result.components) == 1
    assert result.components[0].metadata["start_object"] is None

    rel_types = {r.relationship_type for r in result.relationships}
    assert "TRIGGERS_ON" not in rel_types
    assert "REFERENCES" in rel_types


def test_lwc_parser_extracts_bundle_with_apex_and_schema_imports(
    fixture_tree: Path,
) -> None:
    parser = LWCParser()
    path = (
        fixture_tree / "force-app" / "main" / "default"
        / "lwc" / "accountSummary" / "accountSummary.js-meta.xml"
    )
    assert parser.handles(path)
    result = parser.parse(path)

    assert len(result.components) == 1
    bundle = result.components[0]
    assert bundle.component_type == "LightningComponentBundle"
    assert bundle.api_name == "accountSummary"
    assert bundle.id == "LightningComponentBundle:accountSummary"
    assert bundle.metadata["api_version"] == "62.0"
    assert bundle.metadata["is_exposed"] is True
    assert bundle.metadata["master_label"] == "Account Summary"
    assert bundle.metadata["targets"] == [
        "lightning__RecordPage", "lightning__AppPage",
    ]
    assert bundle.metadata["apex_imports"] == ["AccountHandler"]
    assert bundle.metadata["schema_objects"] == ["Account"]
    assert bundle.metadata["schema_fields"] == ["Account.Region__c"]
    assert bundle.metadata["has_html"] is True
    assert bundle.metadata["has_css"] is False

    apex_refs = [r for r in result.relationships if r.relationship_type == "REFERENCES"]
    assert {r.target_id for r in apex_refs} == {"ApexClass:AccountHandler"}

    obj_refs = [r for r in result.relationships if r.relationship_type == "REFERENCES_OBJECT"]
    assert {r.target_id for r in obj_refs} == {"CustomObject:Account"}


def test_lwc_parser_rejects_field_meta_xml(tmp_path: Path) -> None:
    """A *.field-meta.xml file must not be picked up by LWCParser, even though
    it ends in 'meta.xml' — the bundle-name match guards against that."""
    field = tmp_path / "Region__c.field-meta.xml"
    field.write_text("<CustomField/>", encoding="utf-8")
    assert LWCParser().handles(field) is False


def test_validation_rule_parser_handles_orphan_path(tmp_path: Path) -> None:
    """A misplaced validation rule (not under objects/<X>/validationRules/) still
    parses; it just emits no VALIDATES_ON edge."""
    orphan = tmp_path / "Stray.validationRule-meta.xml"
    orphan.write_text(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<ValidationRule xmlns=\"http://soap.sforce.com/2006/04/metadata\">\n"
        "  <fullName>Stray</fullName>\n"
        "  <active>false</active>\n"
        "  <errorConditionFormula>true</errorConditionFormula>\n"
        "  <errorMessage>...</errorMessage>\n"
        "</ValidationRule>\n",
        encoding="utf-8",
    )
    result = ValidationRuleParser().parse(orphan)
    assert len(result.components) == 1
    assert result.components[0].metadata["object"] == ""
    assert result.relationships == []


# ---------------------------------------------------------------------------
# Orchestrator + index integration
# ---------------------------------------------------------------------------

def test_ingest_directory_indexes_expected_components(built_index) -> None:
    index, result = built_index
    # 3 classes + 2 triggers + 1 object + 2 fields + 1 validation rule
    # + 1 record type + 1 flow + 1 LWC bundle
    assert result.components_indexed >= 12
    stats = index.stats()
    assert stats.get("ApexClass") == 3
    assert stats.get("ApexTrigger") == 2
    assert stats.get("CustomObject") == 1
    assert stats.get("CustomField") == 2
    assert stats.get("ValidationRule") == 1
    assert stats.get("RecordType") == 1
    assert stats.get("Flow") == 1
    assert stats.get("LightningComponentBundle") == 1


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


def test_apex_trigger_emits_references_edge(built_index) -> None:
    """AccountTrigger calls AccountHandler -> REFERENCES edge should resolve."""
    index, _ = built_index
    edges = index.relationships_of("ApexTrigger:AccountTrigger", direction="outgoing")
    references = [e for e in edges if e.relationship_type == "REFERENCES"]
    targets = {e.partner.api_name for e in references}
    assert "AccountHandler" in targets, \
        f"Expected REFERENCES edge to AccountHandler, got: {targets}"
    # Trigger.new should NOT have produced an edge — Trigger is a built-in.
    assert "Trigger" not in targets


def test_apex_class_extends_does_not_double_count_as_reference(built_index) -> None:
    """AccountHandlerExt extends AccountHandler — there should be exactly one
    EXTENDS edge, not also a stray REFERENCES edge to the same target."""
    index, _ = built_index
    edges = index.relationships_of("ApexClass:AccountHandlerExt", direction="outgoing")
    by_type = {}
    for e in edges:
        if e.partner.api_name == "AccountHandler":
            by_type.setdefault(e.relationship_type, 0)
            by_type[e.relationship_type] += 1
    assert by_type.get("EXTENDS") == 1
    assert by_type.get("REFERENCES", 0) == 0, \
        "Parent class should be excluded from REFERENCES (already covered by EXTENDS)"


def test_apex_class_metadata_includes_references_list(built_index) -> None:
    """References extraction should also populate component.metadata.references."""
    index, _ = built_index
    trigger = index.find_by_id("ApexTrigger:AccountTrigger")
    assert trigger is not None
    assert "AccountHandler" in trigger.metadata.get("references", [])


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
            "ApexClass": 3, "ApexTrigger": 2, "CustomObject": 1,
            "CustomField": 2, "ValidationRule": 1, "RecordType": 1,
            "Flow": 1, "LightningComponentBundle": 1,
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

    # Drive the full-retrieve path explicitly — this test is about cleanup,
    # not delta logic.
    result = build_index(
        org_alias="FakeOrg",
        db_path=db_path,
        retrieve_dir=retrieve_dir,
        delta=False,
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
        delta=False,
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
        delta=False,
    )
    assert result.success
    assert retrieve_dir.exists(), "Retrieve dir should be preserved when cleanup is opted out"
