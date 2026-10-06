"""Protocol-level tests for the sf-context MCP server (in-memory client <-> server)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp")

from mcp.shared.memory import create_connected_server_and_client_session  # noqa: E402
from sf_context_engine.mcp_server import build_server  # noqa: E402
from sf_context_engine.service import ContextService  # noqa: E402

EXPECTED_TOOLS = {
    "retrieve_context", "code_search", "semantic_search", "dependency_graph",
    "knowledge_search", "index_status", "build_metadata_index",
    "embed_metadata_index", "embed_knowledge_base", "memory_save",
    "memory_recall", "memory_list",
}
READ_ONLY = {
    "retrieve_context", "code_search", "semantic_search", "dependency_graph",
    "knowledge_search", "index_status", "memory_recall", "memory_list",
}


@pytest.fixture(autouse=True)
def _no_gemini(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)


def _run(db_path: Path, fn):
    """Open an in-memory client session against a fresh server and run `fn(session)`."""
    server = build_server(ContextService(org_alias="TestOrg", tenant_id="t1", db_path=db_path))

    async def go():
        async with create_connected_server_and_client_session(server._mcp_server) as session:
            return await fn(session)

    return asyncio.run(go())


def _payload(result: Any) -> dict[str, Any]:
    assert not result.isError, result
    if getattr(result, "structuredContent", None):
        sc = result.structuredContent
        return sc.get("result", sc) if set(sc) == {"result"} else sc
    return json.loads(result.content[0].text)


def test_lists_expected_tools_with_annotations(tmp_path):
    async def fn(session):
        return (await session.list_tools()).tools

    tools = {t.name: t for t in _run(tmp_path / "db.sqlite", fn)}
    assert set(tools) == EXPECTED_TOOLS
    for name, tool in tools.items():
        assert tool.description, name
        assert tool.annotations is not None, name
        assert bool(tool.annotations.readOnlyHint) == (name in READ_ONLY), name
        assert tool.annotations.destructiveHint is False, name


def test_no_org_write_or_deploy_tools_exposed(tmp_path):
    async def fn(session):
        return {t.name for t in (await session.list_tools()).tools}

    names = _run(tmp_path / "db.sqlite", fn)
    assert not {n for n in names if any(w in n for w in ("deploy", "delete", "apex_execute"))}


def test_index_status_reports_missing_index_and_mock_embedder(tmp_path):
    async def fn(session):
        return _payload(await session.call_tool("index_status", {}))

    out = _run(tmp_path / "missing.sqlite", fn)
    assert out["index_built"] is False
    assert out["embedder_is_mock"] is True
    assert "warning" in out


def test_query_tools_return_structured_error_when_index_missing(tmp_path):
    async def fn(session):
        return (
            _payload(await session.call_tool("code_search", {"query": "Account"})),
            _payload(await session.call_tool("retrieve_context", {"query": "Account"})),
        )

    code, ctx = _run(tmp_path / "missing.sqlite", fn)
    assert "build_metadata_index" in code["error"]
    assert "build_metadata_index" in ctx["error"]


def test_knowledge_base_embed_then_search(tmp_path):
    async def fn(session):
        emb = _payload(await session.call_tool("embed_knowledge_base", {}))
        hits = _payload(await session.call_tool(
            "knowledge_search", {"query": "SOQL query inside a loop", "limit": 3},
        ))
        return emb, hits

    emb, hits = _run(tmp_path / "kb.sqlite", fn)
    assert emb["embedded"] > 0
    assert hits["match_count"] > 0
    assert hits["embedder"].startswith("mock")


def test_memory_save_and_list_roundtrip_is_scoped(tmp_path):
    db = tmp_path / "mem.sqlite"

    async def save(session):
        return _payload(await session.call_tool("memory_save", {
            "type": "project", "name": "prefer-triggers",
            "description": "Client prefers triggers over Flows for complex logic",
            "body": "Prefer Apex triggers.\n\n**Why:** team skillset.\n**How to apply:** default to triggers.",
        }))

    saved = _run(db, save)
    assert saved["saved"] is True and saved["org_alias"] == "TestOrg"

    async def listing(session):
        return _payload(await session.call_tool("memory_list", {}))

    listed = _run(db, listing)
    assert listed["count"] == 1
    assert listed["memories"][0]["name"] == "prefer-triggers"

    # A different org alias on the same DB must not see the org-pinned memory.
    other = build_server(ContextService(org_alias="OtherOrg", tenant_id="t1", db_path=db))

    async def other_listing():
        async with create_connected_server_and_client_session(other._mcp_server) as s:
            return _payload(await s.call_tool("memory_list", {}))

    assert asyncio.run(other_listing())["count"] == 0
