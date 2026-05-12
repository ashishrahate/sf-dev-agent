"""Unit tests for the `sf-agent audit tokens` CLI (Item 2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from rich.console import Console

from sf_dev_agent import audit_cli
from sf_dev_agent.audit import LLMAuditStore, LLMInvocationRecord
from sf_dev_agent.audit_cli import _resolve_since, run_audit_command
from sf_dev_agent.memory import MemoryScope, WorkingMemoryStore
from sf_dev_agent.providers.base import TokenUsage


@pytest.fixture(autouse=True)
def wide_console(monkeypatch: pytest.MonkeyPatch):
    """Force a wide non-terminal console so rich.Table doesn't wrap our
    assertion substrings across cells."""
    monkeypatch.setattr(
        audit_cli, "console",
        Console(force_terminal=False, color_system=None, width=200),
    )


@pytest.fixture
def db_with_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """SQLite seeded with a task + a handful of audit rows. Also redirects
    the package's default_db_path() so `audit_cli` resolves to this DB
    when no --db-path is passed (matches REPL/CLI live behavior)."""
    db = tmp_path / "audit.db"
    wm = WorkingMemoryStore(db)
    wm.create_task(
        "task_one",
        MemoryScope(tenant_id="local-dev", org_alias="OrgA"),
        "a task",
    )
    wm.create_task(
        "task_two",
        MemoryScope(tenant_id="local-dev", org_alias="OrgA"),
        "second task",
    )
    wm.close()
    s = LLMAuditStore(db)
    s.record(LLMInvocationRecord(
        tenant_id="local-dev", org_alias="OrgA", task_id="task_one",
        turn_idx=0, provider="GeminiProvider", model="gemini-2.5-flash",
        usage=TokenUsage(input_tokens=500, output_tokens=50),
        triggered_by_tool=None, emitted_tools=["retrieve_context"],
        started_at=datetime.now(UTC).isoformat(), duration_ms=100,
        mode="plan",
    ))
    s.record(LLMInvocationRecord(
        tenant_id="local-dev", org_alias="OrgA", task_id="task_one",
        turn_idx=1, provider="GeminiProvider", model="gemini-2.5-flash",
        usage=TokenUsage(input_tokens=1500, output_tokens=80),
        triggered_by_tool="retrieve_context", emitted_tools=["code_search"],
        started_at=datetime.now(UTC).isoformat(), duration_ms=200,
        mode="plan",
    ))
    s.record(LLMInvocationRecord(
        tenant_id="local-dev", org_alias="OrgA", task_id="task_two",
        turn_idx=0, provider="GeminiProvider", model="gemini-2.5-flash",
        usage=TokenUsage(input_tokens=300, output_tokens=20),
        triggered_by_tool=None, emitted_tools=[],
        started_at=datetime.now(UTC).isoformat(), duration_ms=80,
        mode="execution",
    ))
    s.close()
    monkeypatch.setattr(
        "sf_dev_agent.audit_cli.default_db_path", lambda: db,
    )
    return db


def test_summary_default_view(
    db_with_calls: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """No flags → summary totals."""
    rc = run_audit_command(["tokens"])
    assert rc == 0
    out = capsys.readouterr().out
    # 500 + 1500 + 300 = 2300 input_tokens; thousands separator → 2,300
    assert "2,300" in out
    # output_tokens 150 total
    assert "150" in out
    assert "calls" in out


def test_summary_filtered_by_tenant(
    db_with_calls: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """A bogus tenant filter zeros everything out."""
    rc = run_audit_command(["tokens", "--tenant", "nope"])
    assert rc == 0
    out = capsys.readouterr().out
    # All metrics should be 0.
    # The 'calls' row reads "calls   0" — substring sufficient.
    assert "calls" in out
    # No 2,300 / 1,500 etc.
    assert "2,300" not in out
    assert "1,500" not in out


def test_by_tool_aggregation(
    db_with_calls: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    rc = run_audit_command(["tokens", "--by", "tool"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "retrieve_context" in out
    # 1500 input_tokens for retrieve_context, formatted
    assert "1,500" in out
    # First-turn rows (NULL trigger) included by default — they render
    # the "(first turn)" placeholder.
    assert "first turn" in out


def test_by_tool_exclude_untriggered(
    db_with_calls: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    rc = run_audit_command([
        "tokens", "--by", "tool", "--exclude-untriggered",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "retrieve_context" in out
    assert "first turn" not in out


def test_by_model_aggregation(
    db_with_calls: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    rc = run_audit_command(["tokens", "--by", "model"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "GeminiProvider" in out
    assert "gemini-2.5-flash" in out


def test_task_view_lists_each_turn(
    db_with_calls: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    rc = run_audit_command(["tokens", "--task", "task_one"])
    assert rc == 0
    out = capsys.readouterr().out
    # Both turns visible.
    assert "task_one" in out
    # turn 0 + turn 1 rendered.
    assert "retrieve_context" in out  # turn 0 emitted, turn 1 triggered by
    # Input token values for each turn.
    assert "500" in out
    assert "1,500" in out


def test_task_view_missing_task_returns_zero_and_warns(
    db_with_calls: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    rc = run_audit_command(["tokens", "--task", "task_nope"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "No LLM calls" in out


def test_missing_db_returns_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the audit DB doesn't exist yet, the CLI prints a hint and exits 0."""
    db = tmp_path / "absent.db"
    monkeypatch.setattr("sf_dev_agent.audit_cli.default_db_path", lambda: db)
    rc = run_audit_command(["tokens"])
    assert rc == 0
    assert "No audit DB found" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# --since shorthand parser
# ---------------------------------------------------------------------------

def test_resolve_since_passthrough_iso() -> None:
    """ISO-8601 inputs pass through verbatim."""
    iso = "2026-05-01T12:34:56+00:00"
    assert _resolve_since(iso) == iso


def test_resolve_since_shorthand_days() -> None:
    out = _resolve_since("7d")
    # The cutoff should be a fresh ISO timestamp roughly 7 days back.
    parsed = datetime.fromisoformat(out)
    delta = datetime.now(UTC) - parsed
    assert timedelta(days=6, hours=23) < delta < timedelta(days=7, hours=1)


def test_resolve_since_shorthand_hours_minutes_seconds() -> None:
    """All four units round-trip into a sensible cutoff."""
    for spec, expect_min, expect_max in [
        ("24h", timedelta(hours=23, minutes=59), timedelta(hours=24, minutes=1)),
        ("90m", timedelta(minutes=89), timedelta(minutes=91)),
        ("30s", timedelta(seconds=29), timedelta(seconds=31)),
    ]:
        cutoff = datetime.fromisoformat(_resolve_since(spec))
        delta = datetime.now(UTC) - cutoff
        assert expect_min < delta < expect_max, (spec, delta)


def test_resolve_since_unparseable_passes_through() -> None:
    """Garbage input falls through as-is (which means SQL gets a literal
    string compare and the natural empty result follows)."""
    assert _resolve_since("not-a-duration") == "not-a-duration"
