"""Unit tests for the metadata-index-backed tools (code_search,
sf_dependency_graph, build_metadata_index) wired into ToolRegistry.

The tests build a fixture index with `ingest_directory`, then invoke each
tool through `ToolRegistry.execute(...)` to mirror the agent loop's path.
No live org is required.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sf_dev_agent.context import ingest_directory
from sf_dev_agent.models.schemas import OrgConnection
from sf_dev_agent.tools.registry import ToolRegistry


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def fixture_tree(tmp_path: Path) -> Path:
    base = tmp_path / "force-app" / "main" / "default"
    _write(base / "classes" / "AccountHandler.cls",
           "public with sharing class AccountHandler {\n"
           "    public static void handle(List<Account> accounts) {}\n"
           "}\n")
    _write(base / "classes" / "AccountHandlerExt.cls",
           "public class AccountHandlerExt extends AccountHandler "
           "implements Schedulable {\n"
           "    public void execute(SchedulableContext sc) {}\n"
           "}\n")
    _write(base / "triggers" / "AccountTrigger.trigger",
           "trigger AccountTrigger on Account (before insert, after update) {}\n")
    _write(base / "objects" / "Account" / "Account.object-meta.xml",
           "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
           "<CustomObject xmlns=\"http://soap.sforce.com/2006/04/metadata\">\n"
           "  <label>Account</label>\n"
           "  <sharingModel>ReadWrite</sharingModel>\n"
           "</CustomObject>\n")
    _write(base / "objects" / "Account" / "fields" / "Region__c.field-meta.xml",
           "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
           "<CustomField xmlns=\"http://soap.sforce.com/2006/04/metadata\">\n"
           "  <fullName>Region__c</fullName>\n"
           "  <label>Region</label>\n"
           "  <type>Picklist</type>\n"
           "</CustomField>\n")
    return tmp_path


@pytest.fixture
def org() -> OrgConnection:
    return OrgConnection(
        tenant_id="t1",
        org_alias="TestOrg",
        org_type="developer",
        instance_url="https://example.salesforce.com",
    )


@pytest.fixture
def populated_registry(fixture_tree: Path, tmp_path: Path, org: OrgConnection):
    """Build the index and return a ToolRegistry pointed at it."""
    db_path = tmp_path / "tools_test.db"
    result = ingest_directory(source_dir=fixture_tree, db_path=db_path)
    assert result.success
    return ToolRegistry(org=org, mock_org=False, index_db_path=db_path)


# ---------------------------------------------------------------------------
# code_search
# ---------------------------------------------------------------------------

def test_code_search_finds_class_by_name(populated_registry: ToolRegistry) -> None:
    result = populated_registry.execute("code_search", {"query": "AccountHandler"})
    names = {hit["api_name"] for hit in result["results"]}
    assert {"AccountHandler", "AccountHandlerExt"}.issubset(names)
    assert all("source" not in hit for hit in result["results"]), \
        "include_source=False (default) should omit source"


def test_code_search_filters_by_component_type(populated_registry: ToolRegistry) -> None:
    result = populated_registry.execute(
        "code_search",
        {"query": "Account", "component_type": "ApexTrigger"},
    )
    assert result["match_count"] == 1
    assert result["results"][0]["component_type"] == "ApexTrigger"
    assert result["results"][0]["api_name"] == "AccountTrigger"


def test_code_search_includes_source_when_requested(populated_registry: ToolRegistry) -> None:
    result = populated_registry.execute(
        "code_search",
        {"query": "AccountHandler", "include_source": True, "limit": 1},
    )
    assert result["match_count"] == 1
    assert "class AccountHandler" in result["results"][0]["source"]


def test_code_search_returns_helpful_error_when_index_missing(
    tmp_path: Path, org: OrgConnection
) -> None:
    registry = ToolRegistry(
        org=org, mock_org=False, index_db_path=tmp_path / "nonexistent.db",
    )
    result = registry.execute("code_search", {"query": "anything"})
    assert "error" in result
    assert "build_metadata_index" in result["error"]


# ---------------------------------------------------------------------------
# sf_dependency_graph
# ---------------------------------------------------------------------------

def test_dependency_graph_outgoing_for_trigger(populated_registry: ToolRegistry) -> None:
    """AccountTrigger should have an outgoing TRIGGERS_ON edge to Account."""
    result = populated_registry.execute(
        "sf_dependency_graph",
        {"component_id": "ApexTrigger:AccountTrigger", "direction": "outgoing"},
    )
    assert result["edge_count"] == 1
    edge = result["edges"][0]
    assert edge["direction"] == "outgoing"
    assert edge["relationship_type"] == "TRIGGERS_ON"
    assert edge["partner"]["id"] == "CustomObject:Account"


def test_dependency_graph_incoming_for_object(populated_registry: ToolRegistry) -> None:
    """Account should see AccountTrigger as an incoming TRIGGERS_ON, plus FIELD_OF for Region__c."""
    result = populated_registry.execute(
        "sf_dependency_graph",
        {"component_id": "CustomObject:Account", "direction": "incoming"},
    )
    types_to_partners = {
        (e["relationship_type"], e["partner"]["api_name"]) for e in result["edges"]
    }
    assert ("TRIGGERS_ON", "AccountTrigger") in types_to_partners
    assert ("FIELD_OF", "Region__c") in types_to_partners


def test_dependency_graph_resolves_by_type_and_name(populated_registry: ToolRegistry) -> None:
    """Caller can pass component_type + api_name when they don't know the canonical id."""
    result = populated_registry.execute(
        "sf_dependency_graph",
        {"component_type": "ApexClass", "api_name": "AccountHandlerExt"},
    )
    assert result["component"]["id"] == "ApexClass:AccountHandlerExt"
    # Outgoing EXTENDS edge to AccountHandler.
    outgoing = [e for e in result["edges"] if e["direction"] == "outgoing"]
    assert any(
        e["relationship_type"] == "EXTENDS" and e["partner"]["api_name"] == "AccountHandler"
        for e in outgoing
    )


def test_dependency_graph_returns_error_for_unknown_component(
    populated_registry: ToolRegistry,
) -> None:
    result = populated_registry.execute(
        "sf_dependency_graph",
        {"component_id": "ApexClass:DoesNotExist"},
    )
    assert "error" in result
    assert "not found" in result["error"].lower()


def test_dependency_graph_rejects_invalid_direction(populated_registry: ToolRegistry) -> None:
    result = populated_registry.execute(
        "sf_dependency_graph",
        {"component_id": "CustomObject:Account", "direction": "sideways"},
    )
    assert "error" in result
    assert "direction" in result["error"].lower()


# ---------------------------------------------------------------------------
# build_metadata_index
# ---------------------------------------------------------------------------

def test_build_metadata_index_in_mock_mode_returns_canned_response(
    org: OrgConnection, tmp_path: Path
) -> None:
    """In mock-org mode, build_metadata_index must NOT shell out to sf — the agent
    loop relies on this for offline testing."""
    registry = ToolRegistry(
        org=org, mock_org=True, index_db_path=tmp_path / "should_not_exist.db",
    )
    result = registry.execute("build_metadata_index", {})
    assert result["success"] is True
    assert result.get("mocked") is True
    assert "skipped in mock-org mode" in result["note"]


# ---------------------------------------------------------------------------
# Tool definition wiring
# ---------------------------------------------------------------------------

def test_new_tools_are_registered(populated_registry: ToolRegistry) -> None:
    """All three new tools should appear in get_tool_definitions()."""
    names = {t["name"] for t in populated_registry.get_tool_definitions()}
    assert {"code_search", "sf_dependency_graph", "build_metadata_index"}.issubset(names)
