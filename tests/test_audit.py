"""Unit tests for the LLM audit store + aggregation views (Item 2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sf_dev_agent.audit import (
    LLMAuditStore,
    LLMInvocationRecord,
    ModelAggregate,
    ToolAggregate,
)
from sf_dev_agent.memory import MemoryScope, WorkingMemoryStore
from sf_dev_agent.providers.base import TokenUsage


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Fresh SQLite per test. WorkingMemoryStore seeds the `tasks` table so
    the FK on `llm_invocations.task_id` is satisfiable for the records we
    write — mirrors the live agent flow where the working-memory row exists
    before the first LLM call's audit row lands."""
    db = tmp_path / "audit.db"
    wm = WorkingMemoryStore(db)
    # Seed two tasks: one in scope, one for cross-scope tests.
    wm.create_task(
        "task_alpha",
        MemoryScope(tenant_id="local-dev", org_alias="OrgA"),
        "first task",
    )
    wm.create_task(
        "task_beta",
        MemoryScope(tenant_id="local-dev", org_alias="OrgB"),
        "second task",
    )
    wm.close()
    return db


@pytest.fixture
def store(db_path: Path):
    s = LLMAuditStore(db_path)
    yield s
    s.close()


# ---------------------------------------------------------------------------
# Record / read round-trip
# ---------------------------------------------------------------------------

def _make_record(**overrides) -> LLMInvocationRecord:
    base = dict(
        tenant_id="local-dev",
        org_alias="OrgA",
        task_id="task_alpha",
        turn_idx=0,
        provider="GeminiProvider",
        model="gemini-2.5-flash",
        usage=TokenUsage(input_tokens=100, output_tokens=20),
        triggered_by_tool=None,
        emitted_tools=[],
        stop_reason="end_turn",
        started_at=datetime.now(UTC).isoformat(),
        duration_ms=42,
        mode="plan",
    )
    base.update(overrides)
    return LLMInvocationRecord(**base)


def test_record_returns_row_id(store: LLMAuditStore) -> None:
    rid = store.record(_make_record())
    assert rid > 0


def test_record_assigns_started_at_when_blank(store: LLMAuditStore) -> None:
    """Empty `started_at` should be filled with current UTC at write time."""
    store.record(_make_record(started_at=""))
    rows = store.list_for_task("task_alpha")
    assert len(rows) == 1
    # ISO-shaped, not empty.
    assert rows[0].started_at
    assert "T" in rows[0].started_at


def test_list_for_task_orders_by_turn_idx(store: LLMAuditStore) -> None:
    """`list_for_task` returns rows in turn-index order, not insert order."""
    store.record(_make_record(turn_idx=2, usage=TokenUsage(input_tokens=300)))
    store.record(_make_record(turn_idx=0, usage=TokenUsage(input_tokens=100)))
    store.record(_make_record(turn_idx=1, usage=TokenUsage(input_tokens=200)))
    rows = store.list_for_task("task_alpha")
    assert [r.turn_idx for r in rows] == [0, 1, 2]
    assert [r.usage.input_tokens for r in rows] == [100, 200, 300]


def test_emitted_tools_json_roundtrip(store: LLMAuditStore) -> None:
    """List of tool names persists as JSON and rehydrates correctly."""
    store.record(_make_record(emitted_tools=["code_search", "retrieve_context"]))
    rows = store.list_for_task("task_alpha")
    assert rows[0].emitted_tools == ["code_search", "retrieve_context"]


def test_record_with_no_emitted_tools_returns_empty_list(store: LLMAuditStore) -> None:
    store.record(_make_record(emitted_tools=[]))
    rows = store.list_for_task("task_alpha")
    assert rows[0].emitted_tools == []


# ---------------------------------------------------------------------------
# Aggregation: by-tool
# ---------------------------------------------------------------------------

def test_aggregate_by_tool_groups_and_sums(store: LLMAuditStore) -> None:
    """Each tool name aggregates calls + tokens; ordering is by total desc."""
    store.record(_make_record(
        triggered_by_tool="retrieve_context",
        usage=TokenUsage(input_tokens=500, output_tokens=50),
    ))
    store.record(_make_record(
        triggered_by_tool="retrieve_context",
        usage=TokenUsage(input_tokens=400, output_tokens=30),
    ))
    store.record(_make_record(
        triggered_by_tool="code_search",
        usage=TokenUsage(input_tokens=200, output_tokens=20),
    ))
    rows = store.aggregate_by_tool()
    # retrieve_context dominates → first
    assert isinstance(rows[0], ToolAggregate)
    assert rows[0].tool_name == "retrieve_context"
    assert rows[0].calls == 2
    assert rows[0].input_tokens == 900
    assert rows[0].output_tokens == 80
    assert rows[1].tool_name == "code_search"


def test_aggregate_by_tool_excludes_untriggered_when_flag_set(
    store: LLMAuditStore,
) -> None:
    """`include_untriggered=False` drops first-turn rows (no parent tool)."""
    store.record(_make_record(
        triggered_by_tool=None,
        usage=TokenUsage(input_tokens=1000),
    ))
    store.record(_make_record(
        triggered_by_tool="code_search",
        usage=TokenUsage(input_tokens=100),
    ))
    rows = store.aggregate_by_tool(include_untriggered=False)
    assert {r.tool_name for r in rows} == {"code_search"}


def test_aggregate_by_tool_scopes_to_tenant(store: LLMAuditStore) -> None:
    """`tenant_id` filter restricts the rows that participate in the sum."""
    store.record(_make_record(
        tenant_id="local-dev",
        triggered_by_tool="code_search",
        usage=TokenUsage(input_tokens=100),
    ))
    store.record(_make_record(
        tenant_id="other-tenant",
        task_id="task_beta",  # FK forces this row to belong elsewhere
        triggered_by_tool="code_search",
        usage=TokenUsage(input_tokens=99999),
    ))
    rows = store.aggregate_by_tool(tenant_id="local-dev")
    assert len(rows) == 1
    assert rows[0].input_tokens == 100


def test_aggregate_by_tool_scopes_to_org_alias(store: LLMAuditStore) -> None:
    store.record(_make_record(
        org_alias="OrgA", task_id="task_alpha",
        triggered_by_tool="code_search",
        usage=TokenUsage(input_tokens=100),
    ))
    store.record(_make_record(
        org_alias="OrgB", task_id="task_beta",
        triggered_by_tool="code_search",
        usage=TokenUsage(input_tokens=200),
    ))
    rows = store.aggregate_by_tool(org_alias="OrgA")
    assert len(rows) == 1
    assert rows[0].input_tokens == 100


def test_aggregate_by_tool_since_filter(store: LLMAuditStore) -> None:
    """`since` filters rows whose `started_at` is older than the lower bound."""
    old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    new = datetime.now(UTC).isoformat()
    store.record(_make_record(
        triggered_by_tool="old_tool", usage=TokenUsage(input_tokens=1),
        started_at=old,
    ))
    store.record(_make_record(
        triggered_by_tool="new_tool", usage=TokenUsage(input_tokens=2),
        started_at=new,
    ))
    cutoff = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    rows = store.aggregate_by_tool(since=cutoff)
    assert {r.tool_name for r in rows} == {"new_tool"}


def test_tool_aggregate_total_tokens() -> None:
    agg = ToolAggregate(
        tool_name="x", calls=1,
        input_tokens=100, output_tokens=20,
        cache_read_tokens=0, cache_write_tokens=0,
    )
    assert agg.total_tokens == 120


# ---------------------------------------------------------------------------
# Aggregation: by-model
# ---------------------------------------------------------------------------

def test_aggregate_by_model_groups_provider_model_pair(store: LLMAuditStore) -> None:
    store.record(_make_record(
        provider="GeminiProvider", model="gemini-2.5-flash",
        usage=TokenUsage(input_tokens=100),
    ))
    store.record(_make_record(
        provider="GeminiProvider", model="gemini-2.5-flash",
        usage=TokenUsage(input_tokens=200),
    ))
    store.record(_make_record(
        provider="AnthropicProvider", model="claude-sonnet-4-6",
        usage=TokenUsage(input_tokens=500),
    ))
    rows = store.aggregate_by_model()
    assert len(rows) == 2
    # Anthropic with 500 input_tokens dominates → first.
    assert isinstance(rows[0], ModelAggregate)
    assert rows[0].provider == "AnthropicProvider"
    # Gemini sums to 300 across the two calls.
    gemini = [r for r in rows if r.provider == "GeminiProvider"][0]
    assert gemini.calls == 2
    assert gemini.input_tokens == 300


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def test_summary_sums_all_fields(store: LLMAuditStore) -> None:
    store.record(_make_record(usage=TokenUsage(
        input_tokens=100, output_tokens=20,
        cache_read_tokens=80, cache_write_tokens=10,
    )))
    store.record(_make_record(usage=TokenUsage(
        input_tokens=200, output_tokens=40,
        cache_read_tokens=120, cache_write_tokens=0,
    )))
    s = store.summary()
    assert s["calls"] == 2
    assert s["input_tokens"] == 300
    assert s["output_tokens"] == 60
    assert s["cache_read_tokens"] == 200
    assert s["cache_write_tokens"] == 10


def test_summary_empty_returns_zeros(store: LLMAuditStore) -> None:
    s = store.summary()
    assert s == {
        "calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }


# ---------------------------------------------------------------------------
# Schema migration / cross-store coexistence
# ---------------------------------------------------------------------------

def test_store_idempotent_on_existing_db(db_path: Path) -> None:
    """Opening the store twice (or a different store in the same file) is
    a no-op — every CREATE is IF NOT EXISTS."""
    LLMAuditStore(db_path).close()
    LLMAuditStore(db_path).close()
    # And opens cleanly alongside WorkingMemoryStore which initialized the
    # schema in the fixture.
    s = LLMAuditStore(db_path)
    s.record(_make_record())
    rows = s.list_for_task("task_alpha")
    s.close()
    assert len(rows) == 1


def test_fk_cascade_drops_invocations_with_task(db_path: Path) -> None:
    """Deleting a task takes its audit rows with it (ON DELETE CASCADE)."""
    s = LLMAuditStore(db_path)
    s.record(_make_record())
    assert len(s.list_for_task("task_alpha")) == 1
    s.close()
    wm = WorkingMemoryStore(db_path)
    wm.delete_task("task_alpha")
    wm.close()
    s = LLMAuditStore(db_path)
    try:
        assert s.list_for_task("task_alpha") == []
    finally:
        s.close()
