"""Integration tests — exercise the tool layer against a real Salesforce org.

Skipped by default; opt in with `pytest -m integration` (or run the file
directly). Each test auto-skips if its prerequisites (sf CLI on PATH, target
org connected) aren't met, so a missing environment is never a hard failure.

Target org resolution:
  1. SF_TEST_ORG_ALIAS env var, or
  2. SF_ORG_ALIAS env var (loaded from .env if present), else skip.

These tests bypass the LLM-driven AgentLoop (which needs API credits and an
interactive approval gate) and validate the agent → sf CLI → org path that
sits underneath it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest
from dotenv import load_dotenv

from sf_dev_agent.models.schemas import OrgConnection
from sf_dev_agent.sf_config import describe_org, derive_api_version, derive_org_type
from sf_dev_agent.tools.registry import ToolRegistry

pytestmark = pytest.mark.integration

load_dotenv()


def _sf_exe() -> str:
    return "sf.cmd" if sys.platform == "win32" else "sf"


def _resolve_test_org_alias() -> str | None:
    return os.environ.get("SF_TEST_ORG_ALIAS") or os.environ.get("SF_ORG_ALIAS")


@pytest.fixture(scope="module")
def sf_cli_available() -> None:
    if shutil.which(_sf_exe()) is None:
        pytest.skip("sf CLI not on PATH")


@pytest.fixture(scope="module")
def test_org_alias(sf_cli_available: None) -> str:
    alias = _resolve_test_org_alias()
    if not alias:
        pytest.skip("Set SF_TEST_ORG_ALIAS or SF_ORG_ALIAS to run integration tests")
    info = describe_org(alias)
    if not info:
        pytest.skip(f"Org '{alias}' is not connected (sf org display returned no result)")
    return alias


@pytest.fixture(scope="module")
def org_connection(test_org_alias: str) -> OrgConnection:
    info = describe_org(test_org_alias) or {}
    return OrgConnection(
        tenant_id="integration-test",
        org_alias=test_org_alias,
        org_type=derive_org_type(test_org_alias),
        instance_url=info.get("instanceUrl", ""),
        api_version=derive_api_version(),
    )


@pytest.fixture
def isolated_workspace(tmp_path, monkeypatch) -> str:
    """Point AGENT_WORKSPACE at a tmp dir so file tools don't touch the real workspace."""
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    return str(tmp_path)


@pytest.fixture
def registry(org_connection: OrgConnection) -> ToolRegistry:
    return ToolRegistry(org=org_connection, mock_org=False)


# ---------------------------------------------------------------------------
# Connection sanity
# ---------------------------------------------------------------------------

def test_target_org_is_connected(test_org_alias: str) -> None:
    """The configured test org should be reachable via sf org display."""
    info = describe_org(test_org_alias)
    assert info is not None
    assert info.get("instanceUrl"), "instanceUrl missing from sf org display output"
    assert info.get("id"), "Org Id missing from sf org display output"


# ---------------------------------------------------------------------------
# Read-only SF CLI tools against a live org
# ---------------------------------------------------------------------------

def test_sf_soql_query_returns_organization_row(registry: ToolRegistry) -> None:
    """SELECT Id FROM Organization always returns exactly one row in any org."""
    result = registry.execute(
        "sf_soql_query",
        {"query": "SELECT Id, Name FROM Organization LIMIT 1"},
    )
    assert isinstance(result, dict), f"Expected dict, got {type(result).__name__}: {result!r}"
    assert result.get("status") == 0, f"Query failed: {result}"

    payload = result.get("result") or {}
    records = payload.get("records") or []
    assert len(records) == 1, f"Expected 1 Organization row, got {len(records)}: {records!r}"
    assert records[0].get("Id"), "Organization row missing Id"


def test_sf_metadata_describe_lists_apex_classes(registry: ToolRegistry) -> None:
    """Listing ApexClass metadata succeeds against a real org (may return empty list)."""
    result = registry.execute(
        "sf_metadata_describe",
        {"component_type": "ApexClass"},
    )
    assert isinstance(result, dict), f"Expected dict, got {type(result).__name__}: {result!r}"
    assert result.get("status") == 0, f"Metadata list failed: {result}"
    # `result.result` is a list of ApexClass entries; even an empty org returns [].
    assert isinstance(result.get("result"), list)


# ---------------------------------------------------------------------------
# Workspace file tools
# ---------------------------------------------------------------------------

def test_file_write_read_roundtrip(
    org_connection: OrgConnection, isolated_workspace: str
) -> None:
    """file_write followed by file_read should return the same content."""
    registry = ToolRegistry(org=org_connection, mock_org=False)
    relative = "force-app/main/default/classes/Probe.cls"
    content = "public class Probe { /* integration test */ }"

    write_result = registry.execute(
        "file_write",
        {"file_path": relative, "content": content},
    )
    assert write_result.get("success") is True, f"file_write failed: {write_result}"
    assert write_result["bytes"] == len(content.encode())

    read_result = registry.execute("file_read", {"file_path": relative})
    assert read_result.get("content") == content
    assert "error" not in read_result


def test_file_write_rejects_path_traversal(
    org_connection: OrgConnection, isolated_workspace: str
) -> None:
    """Paths escaping the workspace root must be rejected."""
    registry = ToolRegistry(org=org_connection, mock_org=False)
    result = registry.execute(
        "file_write",
        {"file_path": "../escaped.txt", "content": "should not write"},
    )
    assert "error" in result, f"Expected traversal error, got: {result}"
    assert "traversal" in result["error"].lower()


# ---------------------------------------------------------------------------
# Sanity: sf CLI version smoke (catches stale or broken installs)
# ---------------------------------------------------------------------------

def test_sf_cli_version_smoke(sf_cli_available: None) -> None:
    proc = subprocess.run(
        [_sf_exe(), "--version"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, f"sf --version failed: {proc.stderr}"
    assert "@salesforce/cli" in proc.stdout.lower() or "sf" in proc.stdout.lower()
