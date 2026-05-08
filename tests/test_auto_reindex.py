"""Tests for auto-reindex after successful file_write / sf_source_deploy.

Covers:
  - `MetadataIndex.delete_outgoing_relationships` and
    `reindex_from_parse_result` API additions.
  - `embed_components(component_ids=...)` filter.
  - `_reindex_files_after_write` free function in agent.py: parse →
    upsert components → wipe-and-replace outgoing edges → optional
    auto-embed (gated on mock_org / GOOGLE_API_KEY / explicit embedder).
  - `AgentLoop._auto_reindex_after_write` hook invoked by
    `_execute_tool` for file_write + sf_source_deploy.
  - `render_reindex_summary` UI helper.

Tests use a real `MetadataIndex` against tmp_path so SQL paths are
exercised; the embedder is stubbed where needed so no real Gemini
calls fire.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from io import StringIO
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from rich.console import Console

from sf_dev_agent import repl_ui
from sf_dev_agent.agent import AgentLoop, _reindex_files_after_write
from sf_dev_agent.context import MetadataIndex
from sf_dev_agent.context.embedders.base import Embedder, MockEmbedder, hash_text
from sf_dev_agent.context.parsers.apex_class import ApexClassParser
from sf_dev_agent.context.parsers.base import (
    ParsedComponent,
    ParsedRelationship,
    ParseResult,
    Parser,
    _reset_for_tests,
    dispatch,
    register,
)
# Eager import so the module-level parser registrations land before any
# test does `parsers.base._reset_for_tests()`.
import sf_dev_agent.context.parsers  # noqa: F401
from sf_dev_agent.memory import MemoryScope, WorkingMemoryStore
from sf_dev_agent.models.schemas import AgentMode, OrgConnection
from sf_dev_agent.providers.base import (
    LLMProvider,
    LLMResponse,
    StreamChunk,
    StreamChunkKind,
    consume_stream,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def org() -> OrgConnection:
    return OrgConnection(
        tenant_id="t1", org_alias="OrgA",
        org_type="developer",
        instance_url="https://example.salesforce.com",
    )


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Tmp DB redirected from default_db_path for the duration of the test."""
    db = tmp_path / "wm.db"
    monkeypatch.setattr(
        "sf_dev_agent.context.default_db_path", lambda: db,
    )
    return db


@pytest.fixture
def workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "sf_dev_agent.paths.agent_workspace", lambda: ws,
    )
    monkeypatch.setenv("AGENT_WORKSPACE", str(ws))
    return ws


@pytest.fixture
def restore_parsers() -> Iterator[None]:
    """Snapshot the parser registry so tests that mutate it don't leak."""
    from sf_dev_agent.context.parsers.base import _REGISTRY
    snapshot = list(_REGISTRY)
    yield
    _REGISTRY.clear()
    _REGISTRY.extend(snapshot)


@pytest.fixture
def repl_ui_capture() -> Iterator[StringIO]:
    """Swap the repl_ui module console for assertions on rendered text."""
    buf = StringIO()
    console = Console(
        file=buf, force_terminal=False, color_system=None, width=200,
    )
    repl_ui.set_console_for_tests(console)
    yield buf
    repl_ui.set_console_for_tests(Console())


def _seed_apex_class(
    index: MetadataIndex, api_name: str, source: str = "",
) -> str:
    component_id = f"ApexClass:{api_name}"
    index.upsert_component(ParsedComponent(
        id=component_id, component_type="ApexClass",
        api_name=api_name, file_path=f"{api_name}.cls",
        source=source or f"public class {api_name} {{ }}",
    ))
    # `upsert_component` doesn't commit; without this the seed is rolled
    # back when the test's `with MetadataIndex(...)` block exits and the
    # subsequent _reindex_files_after_write opens a fresh connection.
    index.commit()
    return component_id


# ---------------------------------------------------------------------------
# 1-3 — _reindex_files_after_write happy-path SQL behavior
# ---------------------------------------------------------------------------

def test_reindex_inserts_new_apex_class(
    db_path: Path, workspace: Path,
) -> None:
    cls_path = workspace / "AccountService.cls"
    cls_path.write_text(
        "public with sharing class AccountService {\n"
        "    public static void doStuff() {}\n"
        "}\n",
        encoding="utf-8",
    )
    summary = _reindex_files_after_write([cls_path], mock_org=True)
    assert summary["components"] == 1
    assert summary["skipped"] == 0
    with MetadataIndex(db_path) as index:
        row = index.find_by_id("ApexClass:AccountService")
        assert row is not None
        assert row.api_name == "AccountService"
        assert "doStuff" in (row.source or "")


def test_reindex_updates_existing_class_source_field(
    db_path: Path, workspace: Path,
) -> None:
    # Pre-seed an old version of the row.
    with MetadataIndex(db_path) as index:
        _seed_apex_class(
            index, "AccountService",
            source="public class AccountService { void old() {} }",
        )

    # Write a new version with a new method, reindex.
    cls_path = workspace / "AccountService.cls"
    new_content = (
        "public with sharing class AccountService {\n"
        "    public void freshMethod() { /* new */ }\n"
        "}\n"
    )
    cls_path.write_text(new_content, encoding="utf-8")
    summary = _reindex_files_after_write([cls_path], mock_org=True)
    assert summary["components"] == 1

    with MetadataIndex(db_path) as index:
        row = index.find_by_id("ApexClass:AccountService")
        assert row is not None
        assert "freshMethod" in (row.source or "")
        assert "old()" not in (row.source or "")


def test_reindex_replaces_stale_outgoing_relationships(
    db_path: Path, workspace: Path,
) -> None:
    # Seed three classes; AlphaClass initially references BetaClass.
    # Single-letter names hit the .isupper() filter in the reference
    # extractor (treated as SOQL-style keywords); use mixed-case.
    with MetadataIndex(db_path) as index:
        _seed_apex_class(index, "AlphaClass")
        _seed_apex_class(index, "BetaClass")
        _seed_apex_class(index, "GammaClass")
        index.upsert_relationship(ParsedRelationship(
            source_id="ApexClass:AlphaClass",
            target_id="ApexClass:BetaClass",
            relationship_type="REFERENCES",
        ))
        index.commit()
        rels = index._conn.execute(
            "SELECT target_id FROM relationships WHERE source_id = ?",
            ("ApexClass:AlphaClass",),
        ).fetchall()
        assert {r["target_id"] for r in rels} == {"ApexClass:BetaClass"}

    # Rewrite AlphaClass to reference GammaClass instead.
    a_path = workspace / "AlphaClass.cls"
    a_path.write_text(
        "public class AlphaClass {\n"
        "    public void use() {\n"
        "        GammaClass g = new GammaClass();\n"
        "        g.run();\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    summary = _reindex_files_after_write([a_path], mock_org=True)
    assert summary["components"] == 1
    assert summary["relationships"] >= 1

    with MetadataIndex(db_path) as index:
        rels = index._conn.execute(
            "SELECT target_id, relationship_type FROM relationships "
            "WHERE source_id = ?",
            ("ApexClass:AlphaClass",),
        ).fetchall()
        targets = {r["target_id"] for r in rels}
        assert "ApexClass:BetaClass" not in targets, (
            "stale REFERENCES should be wiped"
        )
        assert "ApexClass:GammaClass" in targets, (
            "new REFERENCES should be present"
        )


# ---------------------------------------------------------------------------
# 4 — hash-gate verification for updated content
# ---------------------------------------------------------------------------

def test_reindex_resets_embedding_hash_via_hash_gate(
    db_path: Path, workspace: Path,
) -> None:
    """After re-upserting with new content, the stored hash no longer
    matches the current source's hash — next embed run will pick it up."""
    fixed_old_hash = "deadbeef" * 8
    with MetadataIndex(db_path) as index:
        _seed_apex_class(
            index, "AccountService",
            source="public class AccountService { void v1() {} }",
        )
        index._conn.execute(
            "UPDATE components SET embedded_source_hash = ? WHERE id = ?",
            (fixed_old_hash, "ApexClass:AccountService"),
        )
        index._conn.commit()

    cls_path = workspace / "AccountService.cls"
    cls_path.write_text(
        "public class AccountService { void v2_renamed() {} }\n",
        encoding="utf-8",
    )
    _reindex_files_after_write([cls_path], mock_org=True)

    with MetadataIndex(db_path) as index:
        row = index._conn.execute(
            "SELECT * FROM components WHERE id = ?",
            ("ApexClass:AccountService",),
        ).fetchone()
        # Stored hash is still the OLD hash (we didn't auto-clear it —
        # hash-gate works by *comparing* current vs stored on next embed run).
        assert row["embedded_source_hash"] == fixed_old_hash
        # But hash recomputed from the new source no longer matches the
        # stored hash — so the next embed pass will re-embed this row.
        new_text = MetadataIndex._embedding_text(row)
        new_hash = hash_text(new_text)
        assert new_hash != fixed_old_hash


# ---------------------------------------------------------------------------
# 5-8 — defensive paths
# ---------------------------------------------------------------------------

def test_reindex_skips_files_without_parser(
    db_path: Path, workspace: Path,
) -> None:
    notes = workspace / "notes.txt"
    notes.write_text("nothing parseable here\n", encoding="utf-8")
    summary = _reindex_files_after_write([notes], mock_org=True)
    assert summary["components"] == 0
    assert summary["skipped"] == 1


def test_reindex_skips_missing_files(
    db_path: Path, workspace: Path,
) -> None:
    summary = _reindex_files_after_write(
        [workspace / "ghost.cls"], mock_org=True,
    )
    assert summary["components"] == 0
    assert summary["skipped"] == 1


def test_reindex_handles_parser_exception(
    db_path: Path, workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cls_path = workspace / "Boom.cls"
    cls_path.write_text("public class Boom {}\n", encoding="utf-8")

    # Force the dispatched ApexClass parser to raise mid-parse.
    def boom(self: Parser, path: Path) -> ParseResult:
        raise RuntimeError("simulated parse error")
    monkeypatch.setattr(ApexClassParser, "parse", boom)

    summary = _reindex_files_after_write([cls_path], mock_org=True)
    assert summary["components"] == 0
    assert summary["skipped"] == 1
    # No row written despite the crash.
    with MetadataIndex(db_path) as index:
        assert index.find_by_id("ApexClass:Boom") is None


def test_reindex_handles_index_open_failure(
    tmp_path: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Force `MetadataIndex(...)` to raise; reindex must not propagate.

    Cross-platform-safe: monkeypatch the constructor so we don't depend
    on filesystem-permission quirks (Windows is more permissive about
    nonsense paths than POSIX).
    """
    def boom(self: Any, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("simulated DB open failure")
    monkeypatch.setattr(
        "sf_dev_agent.context.MetadataIndex.__init__", boom,
    )
    cls_path = workspace / "X.cls"
    cls_path.write_text("public class X {}\n", encoding="utf-8")
    summary = _reindex_files_after_write([cls_path], mock_org=True)
    # All counts zero; no exception bubbled.
    assert summary == {
        "components": 0, "relationships": 0, "embedded": 0, "skipped": 0,
    }


# ---------------------------------------------------------------------------
# 9-11 — end-to-end through _execute_tool
# ---------------------------------------------------------------------------

class _ScriptedProvider(LLMProvider):
    """Streaming provider that pops scripted items from `script` until empty."""

    def __init__(self, script: list[tuple]) -> None:
        self.script = list(script)
        self.calls = 0

    @property
    def model_name(self) -> str:
        return "scripted"

    def chat(self, **kwargs: Any) -> LLMResponse:
        return consume_stream(self.chat_stream(**kwargs))

    def chat_stream(self, **kwargs: Any) -> Iterator[StreamChunk]:
        self.calls += 1
        if not self.script:
            yield StreamChunk(kind=StreamChunkKind.STOP, stop_reason="end_turn")
            return
        item = self.script.pop(0)
        if item[0] == "text":
            yield StreamChunk(kind=StreamChunkKind.TEXT_DELTA, text=item[1])
            yield StreamChunk(kind=StreamChunkKind.STOP, stop_reason="end_turn")
        elif item[0] == "tool":
            _, name, tool_input = item
            tool_id = f"tu_{self.calls}"
            yield StreamChunk(
                kind=StreamChunkKind.TOOL_USE_START,
                tool_id=tool_id, tool_name=name,
            )
            yield StreamChunk(
                kind=StreamChunkKind.TOOL_USE_END,
                tool_id=tool_id, tool_input=tool_input,
            )
            yield StreamChunk(kind=StreamChunkKind.STOP, stop_reason="tool_use")


@pytest.fixture
def wm(db_path: Path) -> Iterator[WorkingMemoryStore]:
    store = WorkingMemoryStore(db_path)
    yield store
    store.close()


def test_file_write_triggers_reindex_via_execute_tool(
    org: OrgConnection, wm: WorkingMemoryStore,
    db_path: Path, workspace: Path,
) -> None:
    provider = _ScriptedProvider([
        ("tool", "file_write", {
            "file_path": "EndToEnd.cls",
            "content": "public class EndToEnd { void run() {} }\n",
        }),
    ])
    agent = AgentLoop(
        org=org, provider=provider, mock_org=True,
        working_memory=wm, mode=AgentMode.EXECUTION, streaming=True,
    )
    agent.run("create the class")

    with MetadataIndex(db_path) as index:
        row = index.find_by_id("ApexClass:EndToEnd")
        assert row is not None, (
            "file_write hook should have parsed + indexed the new class"
        )


def test_sf_source_deploy_triggers_reindex_for_directory_contents(
    org: OrgConnection, wm: WorkingMemoryStore,
    db_path: Path, workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful sf_source_deploy reindexes every parseable file
    under the supplied source_path."""
    deploy_dir = workspace / "deploy_pkg"
    deploy_dir.mkdir()
    (deploy_dir / "Alpha.cls").write_text(
        "public class Alpha { }\n", encoding="utf-8",
    )
    (deploy_dir / "Beta.cls").write_text(
        "public class Beta { }\n", encoding="utf-8",
    )

    # Stub the registry's executor so we don't actually shell out to sf CLI.
    from sf_dev_agent.tools import registry as registry_mod
    monkeypatch.setattr(
        registry_mod.ToolRegistry,
        "_exec_source_deploy",
        lambda self, **kwargs: {"status": "Succeeded", "deployed_components": 2},
    )

    provider = _ScriptedProvider([
        ("tool", "sf_source_deploy", {"source_path": "deploy_pkg"}),
    ])
    agent = AgentLoop(
        org=org, provider=provider, mock_org=False,
        working_memory=wm, mode=AgentMode.EXECUTION, streaming=True,
    )
    agent.run("deploy the dir")

    with MetadataIndex(db_path) as index:
        assert index.find_by_id("ApexClass:Alpha") is not None
        assert index.find_by_id("ApexClass:Beta") is not None


def test_failed_deploy_does_not_reindex(
    org: OrgConnection, wm: WorkingMemoryStore,
    db_path: Path, workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deploy_dir = workspace / "fail_pkg"
    deploy_dir.mkdir()
    (deploy_dir / "ShouldNotIndex.cls").write_text(
        "public class ShouldNotIndex {}\n", encoding="utf-8",
    )

    from sf_dev_agent.tools import registry as registry_mod
    monkeypatch.setattr(
        registry_mod.ToolRegistry,
        "_exec_source_deploy",
        lambda self, **kwargs: {"error": "deploy failed: ApexCompileError"},
    )

    provider = _ScriptedProvider([
        ("tool", "sf_source_deploy", {"source_path": "fail_pkg"}),
    ])
    agent = AgentLoop(
        org=org, provider=provider, mock_org=False,
        working_memory=wm, mode=AgentMode.EXECUTION, streaming=True,
    )
    agent.run("try to deploy")

    with MetadataIndex(db_path) as index:
        assert index.find_by_id("ApexClass:ShouldNotIndex") is None, (
            "failed deploys must not index local files"
        )


# ---------------------------------------------------------------------------
# 12-15 — auto-embed branch
# ---------------------------------------------------------------------------

class _CountingMockEmbedder(MockEmbedder):
    """MockEmbedder that records every embed() call. Lets tests assert
    the embedder was (or wasn't) invoked."""

    def __init__(self) -> None:
        super().__init__(dim=64)
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[np.ndarray]:
        self.calls.append(list(texts))
        return super().embed(texts)


def test_auto_embed_runs_for_changed_rows(
    db_path: Path, workspace: Path,
) -> None:
    embedder = _CountingMockEmbedder()
    cls_path = workspace / "Greeter.cls"
    cls_path.write_text(
        "public class Greeter { void hi() {} }\n", encoding="utf-8",
    )
    summary = _reindex_files_after_write(
        [cls_path], mock_org=False, embedder=embedder,
    )
    assert summary["components"] == 1
    assert summary["embedded"] == 1
    assert len(embedder.calls) == 1
    # The text passed to the embedder should mention the class name.
    assert any("Greeter" in t for t in embedder.calls[0])

    # Hash now matches the embedded text — next no-op call shouldn't re-embed.
    with MetadataIndex(db_path) as index:
        row = index._conn.execute(
            "SELECT * FROM components WHERE id = ?",
            ("ApexClass:Greeter",),
        ).fetchone()
        text = MetadataIndex._embedding_text(row)
        assert hash_text(text) == row["embedded_source_hash"]


def test_auto_embed_skipped_in_mock_mode(
    db_path: Path, workspace: Path,
) -> None:
    embedder = _CountingMockEmbedder()
    cls_path = workspace / "MockSkip.cls"
    cls_path.write_text("public class MockSkip {}\n", encoding="utf-8")
    summary = _reindex_files_after_write(
        [cls_path], mock_org=True, embedder=embedder,
    )
    assert summary["components"] == 1
    assert summary["embedded"] == 0
    assert embedder.calls == [], "embedder must not be called in mock mode"


def test_auto_embed_skipped_when_no_api_key(
    db_path: Path, workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No embedder injected, GOOGLE_API_KEY absent → auto-embed skips
    cleanly (don't silently fall back to MockEmbedder)."""
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    cls_path = workspace / "NoKey.cls"
    cls_path.write_text("public class NoKey {}\n", encoding="utf-8")
    summary = _reindex_files_after_write([cls_path])  # no mock_org=, no embedder=
    assert summary["components"] == 1
    assert summary["embedded"] == 0


def test_auto_embed_skips_unchanged_content(
    db_path: Path, workspace: Path,
) -> None:
    embedder = _CountingMockEmbedder()
    cls_path = workspace / "Stable.cls"
    cls_path.write_text("public class Stable {}\n", encoding="utf-8")
    _reindex_files_after_write([cls_path], mock_org=False, embedder=embedder)
    assert len(embedder.calls) == 1

    # Second call, identical content — hash-gate kicks in.
    _reindex_files_after_write([cls_path], mock_org=False, embedder=embedder)
    assert len(embedder.calls) == 1, (
        "second pass with unchanged content shouldn't invoke the embedder"
    )


# ---------------------------------------------------------------------------
# 16 — embed_components(component_ids=...) filter
# ---------------------------------------------------------------------------

def test_embed_components_component_ids_filter_restricts_scan(
    db_path: Path,
) -> None:
    """The new component_ids parameter restricts the embed scan."""
    embedder = _CountingMockEmbedder()
    with MetadataIndex(db_path) as index:
        _seed_apex_class(
            index, "First", source="public class First { void m() {} }",
        )
        _seed_apex_class(
            index, "Second", source="public class Second { void m() {} }",
        )
        # Only embed First.
        result = index.embed_components(
            embedder, component_ids=["ApexClass:First"],
        )
        assert result.embedded == 1

    # The embedder was invoked exactly once with First's text.
    assert len(embedder.calls) == 1
    assert any("First" in t for t in embedder.calls[0])
    assert not any("Second" in t for t in embedder.calls[0])

    # And empty list short-circuits without calling the embedder.
    embedder2 = _CountingMockEmbedder()
    with MetadataIndex(db_path) as index:
        result = index.embed_components(embedder2, component_ids=[])
        assert result.embedded == 0
    assert embedder2.calls == []


# ---------------------------------------------------------------------------
# 17 — render_reindex_summary UI
# ---------------------------------------------------------------------------

def test_render_reindex_summary_singular_vs_plural_and_embedded(
    repl_ui_capture: StringIO,
) -> None:
    # 1 component, no relationships, no embedded, no skipped.
    repl_ui.render_reindex_summary(components=1)
    out = repl_ui_capture.getvalue()
    assert "indexed 1 component" in out
    assert "components" not in out  # singular, not plural
    assert "relationship" not in out
    assert "embedded" not in out
    assert "skipped" not in out
    repl_ui_capture.truncate(0)
    repl_ui_capture.seek(0)

    # 2 components, 3 relationships, 1 embedded.
    repl_ui.render_reindex_summary(
        components=2, relationships=3, embedded=1,
    )
    out = repl_ui_capture.getvalue()
    assert "indexed 2 components" in out
    assert "3 relationships" in out
    assert "1 embedded" in out
    repl_ui_capture.truncate(0)
    repl_ui_capture.seek(0)

    # Skipped > 0 surfaces in parens.
    repl_ui.render_reindex_summary(components=1, skipped=2)
    out = repl_ui_capture.getvalue()
    assert "(2 skipped)" in out
    repl_ui_capture.truncate(0)
    repl_ui_capture.seek(0)

    # All zero → no output (caller-facing happy path).
    repl_ui.render_reindex_summary()
    assert repl_ui_capture.getvalue() == ""
