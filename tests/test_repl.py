"""Unit tests for the REPL dispatcher (Phase C.1).

Tests are scoped to `ReplSession._dispatch` and the slash-command
handlers — we never invoke `prompt_toolkit`'s interactive layer here.
That input plumbing is library-tested upstream; our job is the
application logic on top.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sf_dev_agent.memory import MemoryScope, WorkingMemoryStore
from sf_dev_agent.models.schemas import OrgConnection, TaskStatus
from sf_dev_agent.providers.base import LLMProvider, LLMResponse
from sf_dev_agent.repl import (
    ReplSession,
    _print_alert_if_needed,
    _print_banner,
    format_status_dict,
)
from sf_dev_agent.repl_commands import SLASH_COMMANDS, ReplDirective

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class _StubProvider(LLMProvider):
    def __init__(self, name: str = "stub", model: str = "stub-1") -> None:
        self._name = name
        self._model = model

    @property
    def model_name(self) -> str:
        return self._model

    def chat(self, **kwargs: Any) -> LLMResponse:
        return LLMResponse(text_blocks=["stub answer"], stop_reason="end_turn")


@pytest.fixture
def org() -> OrgConnection:
    return OrgConnection(
        tenant_id="local-dev",
        org_alias="OrgA",
        org_type="developer",
        instance_url="https://example.salesforce.com",
    )


@pytest.fixture
def session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, org: OrgConnection,
) -> ReplSession:
    """A ReplSession backed by a tmp working-memory DB."""
    db = tmp_path / "wm.db"
    monkeypatch.setattr(
        "sf_dev_agent.context.default_db_path", lambda: db,
    )
    wm = WorkingMemoryStore(db)
    s = ReplSession(
        org=org, provider=_StubProvider(),
        working_memory=wm, mock_org=False,
    )
    yield s
    wm.close()


# ---------------------------------------------------------------------------
# Slash-command registry
# ---------------------------------------------------------------------------

def test_registry_has_expected_commands() -> None:
    expected = {
        "/help", "/quit", "/exit", "/clear", "/status",
        "/index", "/resume", "/tasks", "/memory",
        "/mock", "/provider", "/verbose", "/mode",
        "/expand", "/tokens",
    }
    assert expected == set(SLASH_COMMANDS.keys())


def test_help_table_lists_every_command(
    session: ReplSession, capsys: pytest.CaptureFixture[str],
) -> None:
    directive = SLASH_COMMANDS["/help"].handler(session, [])
    assert directive == ReplDirective.CONTINUE
    output = capsys.readouterr().out
    for name in SLASH_COMMANDS:
        assert name in output


# ---------------------------------------------------------------------------
# Dispatcher routing
# ---------------------------------------------------------------------------

def test_dispatch_blank_line_continues(session: ReplSession) -> None:
    assert session._dispatch("") == ReplDirective.CONTINUE
    assert session._dispatch("   \t  ") == ReplDirective.CONTINUE


def test_dispatch_unknown_slash_continues(
    session: ReplSession, capsys: pytest.CaptureFixture[str],
) -> None:
    directive = session._dispatch("/nope-not-a-command")
    assert directive == ReplDirective.CONTINUE
    assert "Unknown command" in capsys.readouterr().out


def test_dispatch_quit_returns_quit(session: ReplSession) -> None:
    assert session._dispatch("/quit") == ReplDirective.QUIT
    assert session._dispatch("/exit") == ReplDirective.QUIT


def test_dispatch_freeform_runs_agent(
    session: ReplSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Free-form input creates an AgentLoop and calls .run()."""
    captured: dict[str, Any] = {}

    class _FakeAgent:
        def __init__(self, **kwargs: Any) -> None:
            captured["init"] = kwargs

        def run(self, request: str):
            captured["run"] = request
            from sf_dev_agent.models.schemas import Task
            return Task(
                task_id="task_synth",
                tenant_id="local-dev",
                user_request=request,
            )

    monkeypatch.setattr("sf_dev_agent.repl.AgentLoop", _FakeAgent)
    directive = session._dispatch("describe the Account object")
    assert directive == ReplDirective.CONTINUE
    assert captured["run"] == "describe the Account object"
    # mock_org / provider / org all wired through.
    assert captured["init"]["mock_org"] is False
    assert captured["init"]["org"] is session.org
    # task_id tracked for the /quit extract nudge later.
    assert "task_synth" in session.completed_task_ids


def test_dispatch_freeform_handles_agent_exception(
    session: ReplSession, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An exception inside agent.run shouldn't kill the REPL."""
    class _BoomAgent:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def run(self, request: str):
            raise RuntimeError("simulated agent failure")

    monkeypatch.setattr("sf_dev_agent.repl.AgentLoop", _BoomAgent)
    directive = session._dispatch("anything")
    assert directive == ReplDirective.CONTINUE
    assert "Agent run failed" in capsys.readouterr().out


def test_dispatch_freeform_handles_keyboard_interrupt(
    session: ReplSession, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ctrl+C mid-agent leaves the REPL running."""
    class _InterruptAgent:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def run(self, request: str):
            raise KeyboardInterrupt

    monkeypatch.setattr("sf_dev_agent.repl.AgentLoop", _InterruptAgent)
    directive = session._dispatch("interrupt me")
    assert directive == ReplDirective.CONTINUE
    assert "Interrupted" in capsys.readouterr().out


def test_dispatch_slash_handler_exception_kept_in_repl(
    session: ReplSession, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A handler that raises shouldn't bring down the REPL."""
    def boom(s: ReplSession, argv: list[str]) -> ReplDirective:
        raise RuntimeError("simulated handler failure")

    from sf_dev_agent.repl_commands import SlashCommand
    monkeypatch.setitem(
        SLASH_COMMANDS, "/help",
        SlashCommand(name="/help", summary="boom", handler=boom),
    )
    directive = session._dispatch("/help")
    assert directive == ReplDirective.CONTINUE
    assert "Error in /help" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# /mock toggle
# ---------------------------------------------------------------------------

def test_mock_toggle(session: ReplSession) -> None:
    assert session.mock_org is False
    session._dispatch("/mock on")
    assert session.mock_org is True
    session._dispatch("/mock off")
    assert session.mock_org is False
    session._dispatch("/mock toggle")
    assert session.mock_org is True
    session._dispatch("/mock")  # no arg → toggle
    assert session.mock_org is False


def test_mock_invalid_arg(
    session: ReplSession, capsys: pytest.CaptureFixture[str],
) -> None:
    session._dispatch("/mock garbage")
    assert "Usage" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# /provider switch
# ---------------------------------------------------------------------------

def test_provider_switch(
    session: ReplSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    new_provider = _StubProvider(name="alt", model="alt-1")
    captured: dict[str, Any] = {}

    def fake_create(provider: str, model: str | None = None):
        captured["provider"] = provider
        captured["model"] = model
        return new_provider

    monkeypatch.setattr("sf_dev_agent.providers.create_provider", fake_create)
    monkeypatch.setattr(
        "sf_dev_agent.repl_commands.create_provider", fake_create, raising=False,
    )

    directive = session._dispatch("/provider gemini gemini-2.5-flash")
    assert directive == ReplDirective.CONTINUE
    assert session.provider is new_provider
    assert captured == {"provider": "gemini", "model": "gemini-2.5-flash"}


def test_provider_unknown_name_does_not_change(
    session: ReplSession, capsys: pytest.CaptureFixture[str],
) -> None:
    original = session.provider
    session._dispatch("/provider llama")
    assert session.provider is original
    assert "Unknown provider" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# /verbose toggle
# ---------------------------------------------------------------------------

def test_verbose_toggle(
    session: ReplSession, capsys: pytest.CaptureFixture[str],
) -> None:
    import logging
    root = logging.getLogger()
    original = root.level
    try:
        session._dispatch("/verbose on")
        assert root.level == logging.DEBUG
        session._dispatch("/verbose off")
        assert root.level == logging.INFO
    finally:
        root.setLevel(original)


# ---------------------------------------------------------------------------
# /tasks
# ---------------------------------------------------------------------------

def test_tasks_empty(
    session: ReplSession, capsys: pytest.CaptureFixture[str],
) -> None:
    directive = session._dispatch("/tasks")
    assert directive == ReplDirective.CONTINUE
    assert "No tasks" in capsys.readouterr().out


def test_tasks_excludes_terminal_by_default(
    session: ReplSession, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scope = MemoryScope(tenant_id="local-dev", org_alias="OrgA")

    # Force distinct timestamps so list_tasks ordering is deterministic.
    counter = {"n": 0}

    def fake_now_iso() -> str:
        counter["n"] += 1
        return f"2026-04-28T00:00:{counter['n']:02d}+00:00"

    monkeypatch.setattr("sf_dev_agent.memory.working._now_iso", fake_now_iso)
    monkeypatch.setenv("COLUMNS", "200")

    session.working_memory.create_task("task_inflight", scope, "still going")
    session.working_memory.update_task_status("task_inflight", "planning")
    session.working_memory.create_task("task_finished", scope, "all done")
    session.working_memory.set_result(
        "task_finished", '{"success": true}',
        status=TaskStatus.COMPLETE.value,
    )

    capsys.readouterr()  # drain previous output
    session._dispatch("/tasks")
    out = capsys.readouterr().out
    assert "task_inflight" in out
    assert "task_finished" not in out

    session._dispatch("/tasks --all")
    out = capsys.readouterr().out
    assert "task_inflight" in out
    assert "task_finished" in out


# ---------------------------------------------------------------------------
# Status helpers
# ---------------------------------------------------------------------------

def test_format_status_dict_keys(session: ReplSession) -> None:
    status = format_status_dict(session)
    expected = {
        "tenant", "org", "provider", "model",
        "mock_org", "in-flight tasks", "memories", "index",
    }
    assert expected.issubset(status.keys())
    assert status["mock_org"] == "off"
    assert "OrgA" in status["org"]


def test_status_command_renders(
    session: ReplSession, capsys: pytest.CaptureFixture[str],
) -> None:
    session._dispatch("/status")
    out = capsys.readouterr().out
    # Some content checks — table headers + the org alias should appear.
    assert "Session status" in out
    assert "OrgA" in out


# ---------------------------------------------------------------------------
# /clear
# ---------------------------------------------------------------------------

def test_clear_does_not_drop_state(
    session: ReplSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clearing the screen must NOT touch persistent state."""
    session.completed_task_ids.append("task_x")
    session.mock_org = True

    monkeypatch.setattr("sf_dev_agent.repl_commands.console.clear", lambda: None)
    session._dispatch("/clear")

    assert session.completed_task_ids == ["task_x"]
    assert session.mock_org is True


# ---------------------------------------------------------------------------
# Banner — Solution A (engine state inline)
# ---------------------------------------------------------------------------

def test_banner_includes_engine_state(
    session: ReplSession, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The startup banner now exposes Memory / Tasks / Index counts so the
    user sees engine state on first paint, not just in the bottom toolbar.
    """
    monkeypatch.setenv("COLUMNS", "200")
    _print_banner(session)
    out = capsys.readouterr().out
    assert "Memory:" in out
    assert "Tasks:" in out
    assert "Index:" in out
    # Fresh tmp DB → no index runs → freshness reads "not built".
    assert "not built" in out


# ---------------------------------------------------------------------------
# Alert — Solution B (conditional heads-up panel)
# ---------------------------------------------------------------------------

def test_alert_fires_when_index_not_built(
    session: ReplSession, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Empty index_runs table → loud yellow banner."""
    monkeypatch.setenv("COLUMNS", "200")
    _print_alert_if_needed(session)
    out = capsys.readouterr().out
    assert "heads up" in out
    assert "Index not built" in out


def test_alert_fires_when_in_flight_tasks_exist(
    session: ReplSession, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A task left in a non-terminal state → alert mentions resume."""
    monkeypatch.setenv("COLUMNS", "200")
    scope = MemoryScope(tenant_id="local-dev", org_alias="OrgA")
    session.working_memory.create_task("task_pending_xyz", scope, "draft")
    session.working_memory.update_task_status("task_pending_xyz", "planning")

    _print_alert_if_needed(session)
    out = capsys.readouterr().out
    assert "heads up" in out
    assert "in-flight" in out
    # Task ID is truncated to 8 chars in the preview.
    assert "task_pen" in out
    assert "/resume" in out


def test_alert_silent_when_index_fresh_and_no_tasks(
    session: ReplSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Healthy state — no alert printed at all."""
    import sqlite3
    from datetime import UTC, datetime

    # Seed a recent successful run so check_freshness reads "fresh".
    # Use the canonical schema from context/schema.sql via MetadataIndex.
    from sf_dev_agent.context import MetadataIndex
    db = tmp_path / "wm.db"
    MetadataIndex(db).close()  # runs schema.sql, creates index_runs

    conn = sqlite3.connect(str(db))
    try:
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "INSERT INTO index_runs (org_alias, started_at, completed_at, "
            "component_types, components_count, error) VALUES (?, ?, ?, ?, ?, ?)",
            ("OrgA", now, now, "[]", 0, None),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setenv("COLUMNS", "200")
    _print_alert_if_needed(session)
    out = capsys.readouterr().out
    # No yellow alert panel — the function returned early.
    assert "heads up" not in out


# ---------------------------------------------------------------------------
# Slice B — operating modes in the REPL
# ---------------------------------------------------------------------------

def test_session_defaults_to_plan_mode(session: ReplSession) -> None:
    """Backwards compat — existing call sites that don't pass mode= get plan."""
    from sf_dev_agent.models.schemas import AgentMode
    assert session.mode == AgentMode.PLAN
    assert session.write_allowlist == set()


def test_cycle_mode_order_is_general_plan_execution() -> None:
    """User-chosen order: general → plan → execution → general."""
    from sf_dev_agent.models.schemas import AgentMode
    from sf_dev_agent.repl import cycle_mode

    assert cycle_mode(AgentMode.GENERAL) == AgentMode.PLAN
    assert cycle_mode(AgentMode.PLAN) == AgentMode.EXECUTION
    assert cycle_mode(AgentMode.EXECUTION) == AgentMode.GENERAL


def test_format_mode_label_uses_color_per_mode() -> None:
    """Plan green, general yellow, execution red — colors land in markup."""
    from sf_dev_agent.models.schemas import AgentMode
    from sf_dev_agent.repl import format_mode_label

    assert "green" in format_mode_label(AgentMode.PLAN)
    assert "yellow" in format_mode_label(AgentMode.GENERAL)
    assert "red" in format_mode_label(AgentMode.EXECUTION)


def test_format_status_dict_includes_mode(session: ReplSession) -> None:
    s = format_status_dict(session)
    assert "mode" in s
    assert s["mode"] == "plan"


def test_bottom_toolbar_shows_mode(session: ReplSession) -> None:
    from sf_dev_agent.models.schemas import AgentMode
    from sf_dev_agent.repl import _format_bottom_toolbar

    session.mode = AgentMode.GENERAL
    line = _format_bottom_toolbar(session)
    assert "mode=general" in line


def test_banner_shows_mode(
    session: ReplSession, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from sf_dev_agent.models.schemas import AgentMode

    session.mode = AgentMode.EXECUTION
    monkeypatch.setenv("COLUMNS", "200")
    _print_banner(session)
    out = capsys.readouterr().out
    assert "Mode:" in out
    assert "execution" in out
    assert "Shift+Tab" in out


# ---------------------------------------------------------------------------
# Slice B — /mode slash command
# ---------------------------------------------------------------------------

def test_mode_command_shows_current_when_no_arg(
    session: ReplSession, capsys: pytest.CaptureFixture[str],
) -> None:
    session._dispatch("/mode")
    out = capsys.readouterr().out
    assert "current mode" in out
    assert "plan" in out


def test_mode_command_sets_valid_mode(
    session: ReplSession, capsys: pytest.CaptureFixture[str],
) -> None:
    from sf_dev_agent.models.schemas import AgentMode

    session._dispatch("/mode general")
    assert session.mode == AgentMode.GENERAL
    out = capsys.readouterr().out
    assert "mode set to" in out
    assert "general" in out


def test_mode_command_rejects_invalid(
    session: ReplSession, capsys: pytest.CaptureFixture[str],
) -> None:
    from sf_dev_agent.models.schemas import AgentMode

    original = session.mode
    session._dispatch("/mode bogus")
    out = capsys.readouterr().out
    assert "Unknown mode" in out
    assert session.mode == original  # unchanged


def test_mode_command_each_value_round_trips(session: ReplSession) -> None:
    """All three values are accepted by /mode."""
    from sf_dev_agent.models.schemas import AgentMode

    for m in AgentMode:
        session._dispatch(f"/mode {m.value}")
        assert session.mode == m


# ---------------------------------------------------------------------------
# Slice B — autosuggest on code-change keywords
# ---------------------------------------------------------------------------

def test_looks_like_code_change_detects_keywords() -> None:
    from sf_dev_agent.repl import looks_like_code_change

    assert looks_like_code_change("create a trigger on Account")
    assert looks_like_code_change("Please write the test class")
    assert looks_like_code_change("deploy to sandbox")
    assert looks_like_code_change("FIX the bug in AccountService")  # case-insensitive
    assert looks_like_code_change("refactor.")  # trailing punctuation handled


def test_looks_like_code_change_ignores_pure_questions() -> None:
    from sf_dev_agent.repl import looks_like_code_change

    assert not looks_like_code_change("what is Apex?")
    assert not looks_like_code_change("show me the validation rules")
    assert not looks_like_code_change("")
    assert not looks_like_code_change("how do governor limits work")


def test_autosuggest_no_op_in_plan_mode(
    session: ReplSession, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Plan mode is already safe — no nudge needed."""
    from sf_dev_agent.repl import maybe_autosuggest_plan_mode

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    # Even with a write-shaped ask, plan mode should stay quiet.
    maybe_autosuggest_plan_mode(session, "create a trigger")
    assert "heads up" not in capsys.readouterr().out


def test_autosuggest_no_op_for_pure_question(
    session: ReplSession, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Read-shaped asks shouldn't trigger the nudge even in non-plan mode."""
    from sf_dev_agent.models.schemas import AgentMode
    from sf_dev_agent.repl import maybe_autosuggest_plan_mode

    session.mode = AgentMode.GENERAL
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    maybe_autosuggest_plan_mode(session, "what objects do we have?")
    assert "heads up" not in capsys.readouterr().out


def test_autosuggest_no_op_when_non_tty(
    session: ReplSession, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Don't pollute scripted/CI runs with the nudge."""
    from sf_dev_agent.models.schemas import AgentMode
    from sf_dev_agent.repl import maybe_autosuggest_plan_mode

    session.mode = AgentMode.EXECUTION
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    maybe_autosuggest_plan_mode(session, "create a trigger")
    assert "heads up" not in capsys.readouterr().out


def test_autosuggest_flips_mode_on_yes(
    session: ReplSession, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """User picks 'y' → session.mode flips to PLAN."""
    from sf_dev_agent.models.schemas import AgentMode
    from sf_dev_agent.repl import maybe_autosuggest_plan_mode

    session.mode = AgentMode.GENERAL
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sf_dev_agent.repl.Prompt.ask", lambda *a, **k: "y")
    maybe_autosuggest_plan_mode(session, "create a trigger on Account")
    assert session.mode == AgentMode.PLAN


def test_autosuggest_keeps_mode_on_no(
    session: ReplSession, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Default 'n' (or explicit) → mode unchanged."""
    from sf_dev_agent.models.schemas import AgentMode
    from sf_dev_agent.repl import maybe_autosuggest_plan_mode

    session.mode = AgentMode.EXECUTION
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sf_dev_agent.repl.Prompt.ask", lambda *a, **k: "n")
    maybe_autosuggest_plan_mode(session, "deploy to sandbox")
    assert session.mode == AgentMode.EXECUTION


# ---------------------------------------------------------------------------
# /expand — v2 slice 3 collapsible blocks
# ---------------------------------------------------------------------------

def test_expand_no_arg_shows_usage(
    session: ReplSession, capsys: pytest.CaptureFixture[str],
) -> None:
    session._dispatch("/expand")
    assert "Usage" in capsys.readouterr().out


def test_expand_list_empty_buffer(
    session: ReplSession, capsys: pytest.CaptureFixture[str],
) -> None:
    session._dispatch("/expand --list")
    assert "No tool calls captured" in capsys.readouterr().out


def test_expand_list_populated(
    session: ReplSession, capsys: pytest.CaptureFixture[str],
) -> None:
    session.tool_output_buffer["toolu_aa01"] = {
        "tool_name": "code_search", "payload": "x" * 50, "is_error": False,
    }
    session.tool_output_buffer["toolu_bb02"] = {
        "tool_name": "retrieve_context", "payload": "y" * 200, "is_error": True,
    }
    session._dispatch("/expand --list")
    out = capsys.readouterr().out
    assert "code_search" in out
    assert "retrieve_context" in out
    # Char counts surfaced.
    assert "50" in out
    assert "200" in out


def test_expand_last_prints_most_recent(
    session: ReplSession, capsys: pytest.CaptureFixture[str],
) -> None:
    """Insertion-ordered dict — last inserted entry resolves /expand last."""
    session.tool_output_buffer["toolu_first"] = {
        "tool_name": "a", "payload": "first_payload", "is_error": False,
    }
    session.tool_output_buffer["toolu_second"] = {
        "tool_name": "b", "payload": "second_payload_body", "is_error": False,
    }
    session._dispatch("/expand last")
    out = capsys.readouterr().out
    assert "second_payload_body" in out
    assert "first_payload" not in out


def test_expand_last_empty_buffer(
    session: ReplSession, capsys: pytest.CaptureFixture[str],
) -> None:
    session._dispatch("/expand last")
    assert "No tool calls captured" in capsys.readouterr().out


def test_expand_full_id_match(
    session: ReplSession, capsys: pytest.CaptureFixture[str],
) -> None:
    session.tool_output_buffer["toolu_xyz123"] = {
        "tool_name": "code_search", "payload": "the full output here",
        "is_error": False,
    }
    session._dispatch("/expand toolu_xyz123")
    out = capsys.readouterr().out
    assert "the full output here" in out
    assert "code_search" in out


def test_expand_prefix_match(
    session: ReplSession, capsys: pytest.CaptureFixture[str],
) -> None:
    """Prefix shorter than the full id works when unambiguous."""
    session.tool_output_buffer["toolu_uniqueA"] = {
        "tool_name": "x", "payload": "uniqueA_payload", "is_error": False,
    }
    session._dispatch("/expand toolu_unique")
    out = capsys.readouterr().out
    assert "uniqueA_payload" in out


def test_expand_ambiguous_prefix(
    session: ReplSession, capsys: pytest.CaptureFixture[str],
) -> None:
    session.tool_output_buffer["toolu_shared01"] = {
        "tool_name": "a", "payload": "ignore me",
        "is_error": False,
    }
    session.tool_output_buffer["toolu_shared02"] = {
        "tool_name": "b", "payload": "also ignore",
        "is_error": False,
    }
    session._dispatch("/expand toolu_shared")
    out = capsys.readouterr().out
    assert "Ambiguous" in out
    # Both candidates listed.
    assert "toolu_shared01" in out
    assert "toolu_shared02" in out
    # No payload leaked.
    assert "ignore me" not in out
    assert "also ignore" not in out


def test_expand_no_match(
    session: ReplSession, capsys: pytest.CaptureFixture[str],
) -> None:
    session._dispatch("/expand nope_no_match")
    out = capsys.readouterr().out
    assert "No tool call matching" in out


def test_expand_error_entry_colored_red(
    session: ReplSession, capsys: pytest.CaptureFixture[str],
) -> None:
    """The /expand block uses a red border for errors so the user can tell
    success from failure at a glance even when the payload is JSON."""
    session.tool_output_buffer["toolu_errA"] = {
        "tool_name": "code_search", "payload": "stack trace body",
        "is_error": True,
    }
    session._dispatch("/expand toolu_errA")
    out = capsys.readouterr().out
    assert "stack trace body" in out
    # The "error" label appears in the divider for error entries.
    assert "error" in out


# ---------------------------------------------------------------------------
# /tokens — Item 2 audit views from the REPL
# ---------------------------------------------------------------------------

def test_tokens_delegates_to_audit_cli(
    session: ReplSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The /tokens slash command forwards to `run_audit_command` and threads
    the session's (tenant, org) scope through as default filters."""
    captured: list[list[str]] = []

    def fake_run(argv: list[str]) -> int:
        captured.append(list(argv))
        return 0

    monkeypatch.setattr("sf_dev_agent.audit_cli.run_audit_command", fake_run)
    session._dispatch("/tokens --by tool")
    assert len(captured) == 1
    argv = captured[0]
    # Forwarded first arg is the verb.
    assert argv[0] == "tokens"
    # Session scope defaulted in when not explicit.
    assert "--tenant" in argv
    assert argv[argv.index("--tenant") + 1] == session.org.tenant_id
    assert "--org" in argv
    assert argv[argv.index("--org") + 1] == session.org.org_alias
    # User-supplied flags preserved.
    assert "--by" in argv
    assert argv[argv.index("--by") + 1] == "tool"


def test_tokens_respects_explicit_tenant_override(
    session: ReplSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User-supplied --tenant beats the auto-injected session default."""
    captured: list[list[str]] = []

    def fake_run(argv: list[str]) -> int:
        captured.append(list(argv))
        return 0

    monkeypatch.setattr("sf_dev_agent.audit_cli.run_audit_command", fake_run)
    session._dispatch("/tokens --tenant explicit-tenant")
    argv = captured[0]
    # Auto-inject suppressed; the explicit value rides through.
    assert argv.count("--tenant") == 1
    assert argv[argv.index("--tenant") + 1] == "explicit-tenant"


def test_tokens_systemexit_kept_in_repl(
    session: ReplSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SystemExit from argparse (bad args) doesn't kill the REPL."""
    def boom(argv: list[str]) -> int:
        raise SystemExit(2)

    monkeypatch.setattr("sf_dev_agent.audit_cli.run_audit_command", boom)
    directive = session._dispatch("/tokens --garbage")
    assert directive == ReplDirective.CONTINUE
