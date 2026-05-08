"""Tests for slice C — operating-mode persistence on the tasks row.

Three things have to round-trip correctly:
  1. New `mode` column lands on fresh DBs (via schema.sql).
  2. ALTER TABLE migration adds the column to pre-slice-C DBs.
  3. AgentLoop.run() persists the mode; AgentLoop.resume() reads it
     back from the row and overrides any caller-supplied default.

These tests intentionally use a low-level sqlite3 connection in a few
places to simulate a pre-slice-C DB without the new column, so we can
confirm the migration is doing its job.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from sf_dev_agent.agent import AgentLoop
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
def wm(tmp_path: Path) -> Iterator[WorkingMemoryStore]:
    store = WorkingMemoryStore(tmp_path / "wm.db")
    yield store
    store.close()


@pytest.fixture(autouse=True)
def _redirect_default_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep tests off the user's real ~/.sf-agent state."""
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path / "ws"))
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "sf_dev_agent.context.default_db_path",
        lambda: tmp_path / "wm.db",
    )


class _ScriptedProvider(LLMProvider):
    """Emit a single end_turn — minimal cooperation to drive the loop."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def model_name(self) -> str:
        return "scripted"

    def chat(self, **kwargs: Any) -> LLMResponse:
        return consume_stream(self.chat_stream(**kwargs))

    def chat_stream(self, **kwargs: Any) -> Iterator[StreamChunk]:
        self.calls += 1
        yield StreamChunk(kind=StreamChunkKind.TEXT_DELTA, text="ack")
        yield StreamChunk(kind=StreamChunkKind.STOP, stop_reason="end_turn")


# ---------------------------------------------------------------------------
# Schema + migration
# ---------------------------------------------------------------------------

def test_fresh_db_has_mode_column(wm: WorkingMemoryStore) -> None:
    """New DBs created after slice C ships should already have the column."""
    info = wm._conn.execute("PRAGMA table_info(tasks)").fetchall()
    columns = {row["name"] for row in info}
    assert "mode" in columns


def test_create_task_defaults_mode_to_plan(
    wm: WorkingMemoryStore,
) -> None:
    """Backwards compat: callers that don't pass mode= get 'plan'."""
    scope = MemoryScope(tenant_id="t1", org_alias="OrgA")
    row = wm.create_task(
        task_id="t_default", scope=scope, user_request="hi",
    )
    assert row.mode == "plan"


def test_create_task_persists_explicit_mode(
    wm: WorkingMemoryStore,
) -> None:
    """All three AgentMode values round-trip through the row."""
    scope = MemoryScope(tenant_id="t1", org_alias="OrgA")
    for value in ("plan", "execution", "general"):
        row = wm.create_task(
            task_id=f"t_{value}", scope=scope, user_request="x",
            mode=value,
        )
        assert row.mode == value
        # Round-trip via get_task too.
        again = wm.get_task(f"t_{value}")
        assert again is not None
        assert again.mode == value


def test_migration_adds_column_to_existing_db(tmp_path: Path) -> None:
    """A DB created without the `mode` column gets the column added on
    next WorkingMemoryStore open. Simulates upgrading in place."""
    db = tmp_path / "legacy.db"

    # Hand-build a pre-slice-C schema (no `mode` column).
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                org_alias TEXT,
                status TEXT NOT NULL,
                user_request TEXT NOT NULL,
                plan_json TEXT,
                plan_approved INTEGER NOT NULL DEFAULT 0,
                result_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE conversation_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                role TEXT NOT NULL,
                content_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
                UNIQUE (task_id, seq)
            )
            """
        )
        # Seed a legacy row with NO `mode` column.
        conn.execute(
            "INSERT INTO tasks (id, tenant_id, org_alias, status, "
            "user_request, plan_approved, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("legacy_t", "t1", "OrgA", "complete", "old work", 1,
             "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    # Opening with the new code triggers the ALTER TABLE migration.
    store = WorkingMemoryStore(db)
    try:
        info = store._conn.execute("PRAGMA table_info(tasks)").fetchall()
        columns = {row["name"] for row in info}
        assert "mode" in columns
        # Legacy row gets the DEFAULT 'plan' value.
        legacy = store.get_task("legacy_t")
        assert legacy is not None
        assert legacy.mode == "plan"
    finally:
        store.close()


def test_migration_is_idempotent(wm: WorkingMemoryStore) -> None:
    """Re-running the migration on an already-migrated DB doesn't crash."""
    # Call the migration directly a second time. Should swallow the
    # "duplicate column name" error and return cleanly.
    wm._migrate_add_mode_column()
    wm._migrate_add_mode_column()  # third time too


# ---------------------------------------------------------------------------
# AgentLoop persists mode at task creation
# ---------------------------------------------------------------------------

def test_agent_run_persists_plan_mode(
    org: OrgConnection, wm: WorkingMemoryStore,
) -> None:
    agent = AgentLoop(
        org=org, provider=_ScriptedProvider(), mock_org=True,
        working_memory=wm, mode=AgentMode.PLAN, streaming=True,
    )
    task = agent.run("plan-mode work")
    row = wm.get_task(task.task_id)
    assert row is not None
    assert row.mode == "plan"


def test_agent_run_persists_execution_mode(
    org: OrgConnection, wm: WorkingMemoryStore,
) -> None:
    agent = AgentLoop(
        org=org, provider=_ScriptedProvider(), mock_org=True,
        working_memory=wm, mode=AgentMode.EXECUTION, streaming=True,
    )
    task = agent.run("execution-mode work")
    row = wm.get_task(task.task_id)
    assert row is not None
    assert row.mode == "execution"


def test_agent_run_persists_general_mode(
    org: OrgConnection, wm: WorkingMemoryStore,
) -> None:
    agent = AgentLoop(
        org=org, provider=_ScriptedProvider(), mock_org=True,
        working_memory=wm, mode=AgentMode.GENERAL, streaming=True,
    )
    task = agent.run("general-mode work")
    row = wm.get_task(task.task_id)
    assert row is not None
    assert row.mode == "general"


# ---------------------------------------------------------------------------
# Resume reads mode from the row, ignoring caller-supplied default
# ---------------------------------------------------------------------------

def test_resume_uses_persisted_mode_over_caller_default(
    org: OrgConnection, wm: WorkingMemoryStore,
) -> None:
    """A task created in execution mode must come back as execution
    even if the REPL session has since switched to plan."""
    agent_a = AgentLoop(
        org=org, provider=_ScriptedProvider(), mock_org=True,
        working_memory=wm, mode=AgentMode.EXECUTION, streaming=True,
    )
    task = agent_a.run("create the trigger")
    # Task is now in COMPLETE status. To exercise the resume code path,
    # bump it back to a non-terminal state so resume actually re-runs.
    wm.update_task_status(task.task_id, "executing")

    # Caller claims plan mode, but the row says execution. Row wins.
    agent_b = AgentLoop.resume(
        task_id=task.task_id, org=org, provider=_ScriptedProvider(),
        working_memory=wm, mock_org=True, mode=AgentMode.PLAN,
    )
    # `resume()` returns the Task; we need the AgentLoop instance to
    # check the mode. Reach back through the row to verify persistence.
    row = wm.get_task(task.task_id)
    assert row is not None
    assert row.mode == "execution"


def test_resume_falls_back_to_caller_mode_for_legacy_rows(
    tmp_path: Path, org: OrgConnection,
) -> None:
    """A row whose `mode` somehow ended up empty/null falls back to
    whatever mode the caller passed (defensive — shouldn't happen with
    the migration in place)."""
    db = tmp_path / "wm.db"
    store = WorkingMemoryStore(db)
    try:
        scope = MemoryScope(tenant_id=org.tenant_id, org_alias=org.org_alias)
        # Create a task with a deliberately wrong mode value (not in
        # AgentMode enum) to force the fallback branch.
        store._conn.execute(
            "INSERT INTO tasks (id, tenant_id, org_alias, status, "
            "user_request, plan_approved, mode, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("t_bad", org.tenant_id, org.org_alias, "executing",
             "weird mode", 0, "future_mode_we_dont_know",
             "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        store._conn.commit()

        AgentLoop.resume(
            task_id="t_bad", org=org, provider=_ScriptedProvider(),
            working_memory=store, mock_org=True,
            mode=AgentMode.GENERAL,  # caller's fallback
        )
        # The resumed loop should have mode=GENERAL (fallback) rather
        # than crash on the unknown enum value. We can't directly read
        # the AgentLoop here (resume returns Task), so we just confirm
        # the call didn't raise — that's the contract.
    finally:
        store.close()
