"""Regression tests for defects found by the cross-agent run (docs/mcp/RESULTS.md)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import sf_context_engine as engine
from sf_context_engine import delta
from sf_context_engine.paths import SCHEMA_PATH


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "idx.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()
    conn.close()
    return path


def _put(path: Path, cid: str, ctype: str, name: str, source: str, meta: dict | None = None):
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT OR REPLACE INTO components "
        "(id, component_type, api_name, source, metadata_json, last_indexed_at) "
        "VALUES (?, ?, ?, ?, ?, '2026-01-01T00:00:00Z')",
        (cid, ctype, name, source, json.dumps(meta or {})),
    )
    conn.commit()
    conn.close()


# ------------------------------------------------- custom object suffix (defect 4)


def _object_queries(monkeypatch, entity_records, entity_error=None):
    def tooling(org_alias, query, timeout):
        if "FROM CustomObject" in query:
            return [
                {"Id": "1", "DeveloperName": "Case_Escalation_Event", "NamespacePrefix": None,
                 "LastModifiedDate": "2026-01-01T00:00:00.000+0000"},
                {"Id": "2", "DeveloperName": "Case_SLA_Rule", "NamespacePrefix": None,
                 "LastModifiedDate": "2026-01-01T00:00:00.000+0000"},
                {"Id": "3", "DeveloperName": "Lead_Round_Robin_State", "NamespacePrefix": None,
                 "LastModifiedDate": "2026-01-01T00:00:00.000+0000"},
            ], None
        return [], None

    monkeypatch.setattr(delta, "_query_tooling", tooling)
    monkeypatch.setattr(
        delta, "_query_soql", lambda org_alias, query, timeout: (entity_records, entity_error),
    )


def test_platform_events_and_custom_metadata_keep_their_real_suffix(monkeypatch):
    _object_queries(monkeypatch, [
        {"QualifiedApiName": "Case_Escalation_Event__e", "DeveloperName": "Case_Escalation_Event",
         "NamespacePrefix": None},
        {"QualifiedApiName": "Case_SLA_Rule__mdt", "DeveloperName": "Case_SLA_Rule",
         "NamespacePrefix": None},
        {"QualifiedApiName": "Lead_Round_Robin_State__c", "DeveloperName": "Lead_Round_Robin_State",
         "NamespacePrefix": None},
    ])
    inv = delta.fetch_org_inventory("Org", ["CustomObject"])
    assert {c.api_name for c in inv.components} == {
        "Case_Escalation_Event__e", "Case_SLA_Rule__mdt", "Lead_Round_Robin_State__c",
    }


def test_suffix_lookup_failure_falls_back_to_c(monkeypatch):
    _object_queries(monkeypatch, [], entity_error="boom")
    inv = delta.fetch_org_inventory("Org", ["CustomObject"])
    assert {c.api_name for c in inv.components} == {
        "Case_Escalation_Event__c", "Case_SLA_Rule__c", "Lead_Round_Robin_State__c",
    }


def test_standard_object_with_same_developer_name_is_not_used(monkeypatch):
    # An EntityDefinition row without a custom suffix must not override the __c fallback.
    _object_queries(monkeypatch, [
        {"QualifiedApiName": "Case_SLA_Rule", "DeveloperName": "Case_SLA_Rule",
         "NamespacePrefix": None},
    ])
    inv = delta.fetch_org_inventory("Org", ["CustomObject"])
    assert "Case_SLA_Rule__c" in {c.api_name for c in inv.components}


# ------------------------------------------------- flow active version (defect 1)


def test_flow_metadata_records_active_version_and_explains_the_draft(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _put(db, "Flow:Billing", "Flow", "Billing", "<Flow/>", {"status": "Draft", "label": "Billing"})
    _put(db, "Flow:Never_Activated", "Flow", "Never_Activated", "<Flow/>", {"status": "Draft"})
    monkeypatch.setattr(engine, "fetch_flow_versions", lambda *a, **k: ({
        "Billing": {"is_active": True, "active_version": 8, "latest_version": 9,
                    "latest_status": "Draft"},
        "Never_Activated": {"is_active": False, "active_version": None, "latest_version": 1,
                            "latest_status": "Draft"},
    }, None))

    assert engine._annotate_flow_versions("Org", db) is None

    conn = sqlite3.connect(db)
    meta = {r[0]: json.loads(r[1]) for r in conn.execute(
        "SELECT id, metadata_json FROM components WHERE component_type = 'Flow'")}
    assert meta["Flow:Billing"]["is_active"] is True
    assert meta["Flow:Billing"]["active_version"] == 8
    assert "v8 is the active version" in meta["Flow:Billing"]["version_note"]
    assert meta["Flow:Billing"]["label"] == "Billing"  # existing fields preserved
    assert meta["Flow:Never_Activated"]["is_active"] is False
    assert "version_note" not in meta["Flow:Never_Activated"]


def test_flow_lookup_failure_leaves_index_untouched(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _put(db, "Flow:Billing", "Flow", "Billing", "<Flow/>", {"status": "Draft"})
    monkeypatch.setattr(engine, "fetch_flow_versions", lambda *a, **k: ({}, "no access"))
    assert engine._annotate_flow_versions("Org", db) == "no access"
    conn = sqlite3.connect(db)
    assert json.loads(conn.execute("SELECT metadata_json FROM components").fetchone()[0]) == {
        "status": "Draft",
    }


# ------------------------------------------------- what changed (defect 3)


def test_build_index_reports_added_modified_and_removed(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _put(db, "ApexClass:Keep", "ApexClass", "Keep", "class Keep {}")
    _put(db, "ApexClass:Edit", "ApexClass", "Edit", "class Edit {}")
    _put(db, "ApexClass:Gone", "ApexClass", "Gone", "class Gone {}")

    def fake_full(**kwargs):
        _put(db, "ApexClass:Edit", "ApexClass", "Edit", "class Edit { /* changed */ }")
        _put(db, "ApexClass:New", "ApexClass", "New", "class New {}")
        conn = sqlite3.connect(db)
        conn.execute("DELETE FROM components WHERE id = 'ApexClass:Gone'")
        conn.commit()
        conn.close()
        return engine.IndexBuildResult(success=True, db_path=db)

    monkeypatch.setattr(engine, "_build_index_full", fake_full)
    result = engine.build_index("Org", db_path=db, delta=False, component_types=["ApexClass"])

    assert result.added == ["ApexClass:New"]
    assert result.modified == ["ApexClass:Edit"]
    assert result.removed == ["ApexClass:Gone"]


def test_failed_build_does_not_report_changes(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setattr(
        engine, "_build_index_full",
        lambda **kw: engine.IndexBuildResult(success=False, db_path=db, retrieve_error="x"),
    )
    result = engine.build_index("Org", db_path=db, delta=False, component_types=["ApexClass"])
    assert (result.added, result.modified, result.removed) == ([], [], [])
