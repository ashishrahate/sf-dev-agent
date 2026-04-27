"""Unit tests for WorkingMemoryStore + ConversationLog + AgentLoop integration.

Covers slice 2a — task state and conversation transcript persistence with
resume-from-crash semantics. No LLM API calls (fake provider).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sf_dev_agent.agent import AgentLoop
from sf_dev_agent.memory import (
    ConversationLog,
    MemoryScope,
    WorkingMemoryStore,
)
from sf_dev_agent.models.schemas import OrgConnection, TaskStatus
from sf_dev_agent.providers.base import LLMProvider, LLMResponse, ToolCall

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path: Path) -> WorkingMemoryStore:
    db = tmp_path / "working.db"
    s = WorkingMemoryStore(db)
    yield s
    s.close()


@pytest.fixture
def scope() -> MemoryScope:
    return MemoryScope(tenant_id="t1", org_alias="OrgA")


@pytest.fixture
def org() -> OrgConnection:
    return OrgConnection(
        tenant_id="t1",
        org_alias="OrgA",
        org_type="developer",
        instance_url="https://example.salesforce.com",
    )


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------

def test_opening_store_creates_tables(tmp_path: Path) -> None:
    db = tmp_path / "fresh.db"
    with WorkingMemoryStore(db) as s:
        names = {
            r["name"] for r in s._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"tasks", "conversation_messages"}.issubset(names)


def test_schema_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "again.db"
    WorkingMemoryStore(db).close()
    WorkingMemoryStore(db).close()  # Should not raise.


def test_coexists_with_other_stores(tmp_path: Path) -> None:
    """Working memory shares the SQLite file with index, knowledge, and project memory."""
    from sf_dev_agent.context import KnowledgeBase, MetadataIndex
    from sf_dev_agent.memory import MemoryStore

    db = tmp_path / "shared.db"
    MetadataIndex(db).close()
    KnowledgeBase(db).close()
    MemoryStore(db).close()
    WorkingMemoryStore(db).close()

    with WorkingMemoryStore(db) as s:
        names = {
            r["name"] for r in s._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {
            "components", "knowledge_entries", "memories",
            "tasks", "conversation_messages",
        }.issubset(names)


# ---------------------------------------------------------------------------
# Task CRUD
# ---------------------------------------------------------------------------

def test_create_task_inserts_and_returns_row(
    store: WorkingMemoryStore, scope: MemoryScope
) -> None:
    row = store.create_task(
        task_id="task_001",
        scope=scope,
        user_request="Build me an Account trigger",
    )
    assert row.id == "task_001"
    assert row.tenant_id == "t1"
    assert row.org_alias == "OrgA"
    assert row.status == "planning"
    assert row.plan_approved is False
    assert row.plan_json is None
    assert row.result_json is None


def test_create_task_idempotent_on_pk_collision(
    store: WorkingMemoryStore, scope: MemoryScope
) -> None:
    first = store.create_task("task_x", scope, "first request")
    second = store.create_task("task_x", scope, "different request")
    assert first.id == second.id
    # Existing row is returned untouched — second create does NOT overwrite.
    assert second.user_request == "first request"


def test_create_task_rejects_invalid_args(
    store: WorkingMemoryStore, scope: MemoryScope
) -> None:
    with pytest.raises(ValueError):
        store.create_task("", scope, "x")
    with pytest.raises(ValueError):
        store.create_task("t", MemoryScope(tenant_id=""), "x")
    with pytest.raises(ValueError):
        store.create_task("t", scope, "  ")


def test_update_status_stamps_completed_at_on_terminal(
    store: WorkingMemoryStore, scope: MemoryScope
) -> None:
    store.create_task("task_a", scope, "x")
    store.update_task_status("task_a", "executing")
    assert store.get_task("task_a").completed_at is None

    store.update_task_status("task_a", "complete")
    row = store.get_task("task_a")
    assert row.status == "complete"
    assert row.completed_at is not None


def test_set_plan_persists_json(store: WorkingMemoryStore, scope: MemoryScope) -> None:
    store.create_task("task_p", scope, "x")
    plan = {"summary": "do thing", "steps": []}
    store.set_plan("task_p", json.dumps(plan))

    row = store.get_task("task_p")
    assert row.plan_json is not None
    assert json.loads(row.plan_json) == plan


def test_set_plan_approved_flips_flag(
    store: WorkingMemoryStore, scope: MemoryScope
) -> None:
    store.create_task("task_pa", scope, "x")
    assert store.get_task("task_pa").plan_approved is False
    store.set_plan_approved("task_pa", True)
    assert store.get_task("task_pa").plan_approved is True
    store.set_plan_approved("task_pa", False)
    assert store.get_task("task_pa").plan_approved is False


def test_set_result_writes_result_status_and_completed_at(
    store: WorkingMemoryStore, scope: MemoryScope
) -> None:
    store.create_task("task_r", scope, "x")
    store.set_result(
        "task_r", json.dumps({"success": True}), status="complete",
    )
    row = store.get_task("task_r")
    assert row.status == "complete"
    assert json.loads(row.result_json) == {"success": True}
    assert row.completed_at is not None


# ---------------------------------------------------------------------------
# Scope filter on list_tasks
# ---------------------------------------------------------------------------

def test_list_tasks_scope_strict_on_tenant(store: WorkingMemoryStore) -> None:
    s1 = MemoryScope(tenant_id="t1", org_alias="OrgA")
    s2 = MemoryScope(tenant_id="t2", org_alias="OrgZ")
    store.create_task("a", s1, "t1 work")
    store.create_task("z", s2, "t2 work")

    rows = store.list_tasks(s1)
    ids = {r.id for r in rows}
    assert "a" in ids and "z" not in ids


def test_list_tasks_includes_cross_org_rows(store: WorkingMemoryStore) -> None:
    cross = MemoryScope(tenant_id="t1", org_alias=None)
    org_a = MemoryScope(tenant_id="t1", org_alias="OrgA")
    org_b = MemoryScope(tenant_id="t1", org_alias="OrgB")
    store.create_task("g1", cross, "global setup")
    store.create_task("a1", org_a, "OrgA work")

    a_ids = {r.id for r in store.list_tasks(org_a)}
    b_ids = {r.id for r in store.list_tasks(org_b)}
    assert "g1" in a_ids and "a1" in a_ids
    assert "g1" in b_ids and "a1" not in b_ids


def test_list_tasks_status_filter(
    store: WorkingMemoryStore, scope: MemoryScope
) -> None:
    store.create_task("p1", scope, "x")
    store.create_task("p2", scope, "y")
    store.update_task_status("p2", "complete")

    planning = store.list_tasks(scope, status="planning")
    assert {r.id for r in planning} == {"p1"}
    completed = store.list_tasks(scope, status="complete")
    assert {r.id for r in completed} == {"p2"}


# ---------------------------------------------------------------------------
# Conversation transcript
# ---------------------------------------------------------------------------

def test_append_messages_assigns_increasing_seq(
    store: WorkingMemoryStore, scope: MemoryScope
) -> None:
    store.create_task("task_m", scope, "x")
    s0 = store.append_message("task_m", "user", "hi")
    s1 = store.append_message("task_m", "assistant", "hello")
    s2 = store.append_message("task_m", "user", "more")
    assert s0 == 0 and s1 == 1 and s2 == 2


def test_load_messages_round_trips_string_and_blocks(
    store: WorkingMemoryStore, scope: MemoryScope
) -> None:
    store.create_task("task_rt", scope, "x")
    store.append_message("task_rt", "user", "plain text")
    store.append_message("task_rt", "assistant", [
        {"type": "text", "text": "ok"},
        {"type": "tool_use", "id": "t1", "name": "noop", "input": {}},
    ])
    store.append_message("task_rt", "user", [
        {"type": "tool_result", "tool_use_id": "t1", "content": "done"},
    ])

    msgs = store.load_messages("task_rt")
    assert len(msgs) == 3
    assert msgs[0] == {"role": "user", "content": "plain text"}
    assert msgs[1]["role"] == "assistant"
    assert isinstance(msgs[1]["content"], list)
    assert msgs[1]["content"][1]["name"] == "noop"
    assert msgs[2]["content"][0]["type"] == "tool_result"


def test_append_message_rejects_unknown_role(
    store: WorkingMemoryStore, scope: MemoryScope
) -> None:
    store.create_task("task_role", scope, "x")
    with pytest.raises(ValueError):
        store.append_message("task_role", "system", "nope")


# ---------------------------------------------------------------------------
# FK CASCADE
# ---------------------------------------------------------------------------

def test_delete_task_cascades_to_messages(
    store: WorkingMemoryStore, scope: MemoryScope
) -> None:
    store.create_task("task_d", scope, "x")
    store.append_message("task_d", "user", "msg1")
    store.append_message("task_d", "assistant", "msg2")
    assert store.message_count("task_d") == 2

    deleted = store.delete_task("task_d")
    assert deleted is True
    assert store.get_task("task_d") is None
    assert store.message_count("task_d") == 0


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def test_stats_counts_by_status(
    store: WorkingMemoryStore, scope: MemoryScope
) -> None:
    store.create_task("a", scope, "x")
    store.create_task("b", scope, "y")
    store.update_task_status("b", "complete")
    stats = store.stats(scope=scope)
    assert stats == {"planning": 1, "complete": 1}


# ---------------------------------------------------------------------------
# ConversationLog wrapper
# ---------------------------------------------------------------------------

def test_conversation_log_without_store_acts_like_list() -> None:
    log = ConversationLog(task_id="t1")
    assert len(log) == 0
    assert not log

    log.append({"role": "user", "content": "a"})
    log.append({"role": "assistant", "content": "b"})
    assert len(log) == 2
    assert log[0]["role"] == "user"
    assert [m["content"] for m in log] == ["a", "b"]
    assert log.as_messages() == [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
    ]


def test_conversation_log_writes_through_to_store(
    store: WorkingMemoryStore, scope: MemoryScope
) -> None:
    store.create_task("task_log", scope, "x")
    log = ConversationLog(task_id="task_log", store=store)

    log.append({"role": "user", "content": "first"})
    log.append({"role": "assistant", "content": [{"type": "text", "text": "ok"}]})

    persisted = store.load_messages("task_log")
    assert len(persisted) == 2
    assert persisted[0]["content"] == "first"
    assert persisted[1]["content"][0]["text"] == "ok"


def test_conversation_log_seed_does_not_rewrite_store(
    store: WorkingMemoryStore, scope: MemoryScope
) -> None:
    """Resuming with seed= must NOT duplicate already-persisted messages."""
    store.create_task("task_seed", scope, "x")
    store.append_message("task_seed", "user", "already-stored")

    log = ConversationLog(
        task_id="task_seed",
        store=store,
        seed=store.load_messages("task_seed"),
    )
    # In-memory state mirrors what's on disk.
    assert len(log) == 1
    assert log[0]["content"] == "already-stored"
    # Disk wasn't touched a second time.
    assert store.message_count("task_seed") == 1


def test_conversation_log_persistence_failure_does_not_lose_in_memory_data(
    tmp_path: Path, scope: MemoryScope
) -> None:
    """A SQLite write blowup keeps the in-memory list intact (best-effort write)."""
    db = tmp_path / "wm.db"
    store = WorkingMemoryStore(db)
    store.create_task("task_fail", scope, "x")
    store.close()  # Force later writes to fail.

    log = ConversationLog(task_id="task_fail", store=store)
    log.append({"role": "user", "content": "still here"})
    assert len(log) == 1
    assert log[0]["content"] == "still here"


# ---------------------------------------------------------------------------
# AgentLoop integration
# ---------------------------------------------------------------------------

class _FakeProvider(LLMProvider):
    """Replays a scripted sequence of LLMResponses for testing."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []

    @property
    def model_name(self) -> str:
        return "fake:test"

    def chat(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int = 16384,
    ) -> LLMResponse:
        self.calls.append(list(messages))
        if not self._responses:
            return LLMResponse(text_blocks=["done"], stop_reason="end_turn")
        return self._responses.pop(0)


def test_agent_run_persists_task_lifecycle(
    tmp_path: Path, org: OrgConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run a full plan→approve→execute cycle through AgentLoop with a fake LLM
    and verify every state transition + every conversation message landed
    in the working-memory store.
    """
    db = tmp_path / "agent_wm.db"
    store = WorkingMemoryStore(db)

    # Auto-approve the plan when the agent prompts the user.
    from sf_dev_agent import agent as agent_mod
    monkeypatch.setattr(agent_mod.Prompt, "ask", lambda *a, **k: "yes")

    # Scripted LLM behavior:
    #   1. Planning phase: call submit_plan, then end_turn.
    #   2. Execution phase: just end_turn (no tools).
    plan_input = {
        "summary": "no-op plan",
        "steps": [{
            "step_number": 1,
            "action": "describe",
            "target": "Account",
            "mode": "read",
            "risk": "low",
            "description": "no-op",
        }],
        "preflight_checks": [],
        "risk_assessment": "low",
        "risk_reasoning": "read-only",
        "rollback_strategy": "none required",
    }
    provider = _FakeProvider([
        LLMResponse(
            text_blocks=["Submitting plan."],
            tool_calls=[ToolCall(id="tu_1", name="submit_plan", input=plan_input)],
            stop_reason="tool_use",
        ),
        LLMResponse(text_blocks=["Plan submitted."], stop_reason="end_turn"),
        LLMResponse(text_blocks=["All done."], stop_reason="end_turn"),
    ])

    agent = AgentLoop(
        org=org, provider=provider, max_iterations=10,
        mock_org=True, working_memory=store,
    )
    task = agent.run("Describe the Account object")

    # Task was persisted with the full lifecycle.
    row = store.get_task(task.task_id)
    assert row is not None
    assert row.status == TaskStatus.COMPLETE.value
    assert row.plan_approved is True
    assert row.plan_json is not None
    assert json.loads(row.plan_json)["summary"] == "no-op plan"
    assert row.result_json is not None
    assert json.loads(row.result_json)["success"] is True
    assert row.completed_at is not None

    # Conversation transcript persisted in seq order.
    msgs = store.load_messages(task.task_id)
    assert len(msgs) >= 4  # user request + assistant + tool_results + post-approval user
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == "Describe the Account object"
    # The "Plan approved. Proceed with execution." message must be present.
    contents = [m["content"] for m in msgs]
    assert any(
        isinstance(c, str) and "Plan approved" in c for c in contents
    )

    store.close()


def test_agent_run_without_working_memory_still_works(
    org: OrgConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persistence is opt-in. AgentLoop must run cleanly without a store."""
    from sf_dev_agent import agent as agent_mod
    monkeypatch.setattr(agent_mod.Prompt, "ask", lambda *a, **k: "no")

    provider = _FakeProvider([
        LLMResponse(text_blocks=["I will just answer directly."], stop_reason="end_turn"),
    ])
    agent = AgentLoop(
        org=org, provider=provider, max_iterations=5,
        mock_org=True, working_memory=None,
    )
    task = agent.run("trivial question")
    assert task is not None
    # No plan was produced -> agent treats the run as complete without approval.
    assert task.status == TaskStatus.COMPLETE


def test_agent_run_persists_rejected_plan_as_failed(
    tmp_path: Path, org: OrgConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "rejected.db"
    store = WorkingMemoryStore(db)

    from sf_dev_agent import agent as agent_mod
    monkeypatch.setattr(agent_mod.Prompt, "ask", lambda *a, **k: "no")

    plan_input = {
        "summary": "scary plan",
        "steps": [],
        "preflight_checks": [],
        "risk_assessment": "high",
        "risk_reasoning": "deletes all the things",
        "rollback_strategy": "pray",
    }
    provider = _FakeProvider([
        LLMResponse(
            text_blocks=["Submitting plan."],
            tool_calls=[ToolCall(id="tu_1", name="submit_plan", input=plan_input)],
            stop_reason="tool_use",
        ),
        LLMResponse(text_blocks=["Plan submitted."], stop_reason="end_turn"),
    ])

    agent = AgentLoop(
        org=org, provider=provider, max_iterations=5,
        mock_org=True, working_memory=store,
    )
    task = agent.run("delete production")

    row = store.get_task(task.task_id)
    assert row is not None
    assert row.status == TaskStatus.FAILED.value
    assert row.plan_approved is False
    assert row.completed_at is not None  # terminal state stamped

    store.close()
