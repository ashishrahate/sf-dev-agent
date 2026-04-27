"""Unit tests for the `sf-agent resume` CLI verb (Phase B.3).

The capability is `AgentLoop.resume()` from Wave 8 slice 2b — these tests
cover the CLI plumbing on top: argparse, --list rendering, --latest
resolution, OrgConnection wiring from env, error paths.

Underlying resume behavior is exercised by `test_working_memory.py`;
we mock `AgentLoop.resume` here to verify the CLI dispatches with the
right arguments without spinning up a full agent loop.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from sf_dev_agent.memory import MemoryScope, WorkingMemoryStore
from sf_dev_agent.models.schemas import TaskStatus


@pytest.fixture
def org_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wire the env so resume_cli's _build_org has what it needs."""
    monkeypatch.setenv("SF_ORG_ALIAS", "OrgA")
    monkeypatch.setenv("SF_ORG_TYPE", "developer")
    monkeypatch.setenv("SF_INSTANCE_URL", "https://example.salesforce.com")
    monkeypatch.setenv("SF_API_VERSION", "62.0")
    monkeypatch.setenv("GOOGLE_API_KEY", "AIzaSy-fake")
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def db_with_tasks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Seed a working-memory DB with a mix of in-flight and terminal tasks.

    Patches `_now_iso` with a counter so each task gets a distinct
    created_at — without this, task creation can tie at second-precision
    and `list_tasks` ordering becomes implementation-defined.
    """
    db = tmp_path / "wm.db"
    monkeypatch.setattr(
        "sf_dev_agent.context.default_db_path",
        lambda: db,
    )

    # Drive the WorkingMemoryStore's clock from a counter so every task
    # gets a distinct ISO-second timestamp.
    counter = {"n": 0}

    def fake_now_iso() -> str:
        counter["n"] += 1
        return f"2026-04-27T00:00:{counter['n']:02d}+00:00"

    monkeypatch.setattr(
        "sf_dev_agent.memory.working._now_iso", fake_now_iso,
    )

    scope = MemoryScope(tenant_id="local-dev", org_alias="OrgA")

    plan_input = {
        "summary": "x", "steps": [], "preflight_checks": [],
        "risk_assessment": "low", "risk_reasoning": "x",
        "rollback_strategy": "none",
    }

    # Render the table at a fixed width during tests so the rich auto-
    # sizer doesn't truncate Task IDs in pytest's narrow terminal.
    monkeypatch.setenv("COLUMNS", "200")

    with WorkingMemoryStore(db) as store:
        # Order matters here — each call uses the next ISO second.
        # Older planning task (created first → smallest timestamp).
        store.create_task("task_old_planning", scope, "older planning task")
        store.update_task_status("task_old_planning", "planning")

        # In-flight awaiting_approval (created next → larger timestamp,
        # so this is the "latest" in-flight task).
        store.create_task("task_awaiting", scope, "build a dedup trigger")
        store.set_plan("task_awaiting", json.dumps(plan_input))
        store.update_task_status("task_awaiting", "awaiting_approval")

        # Terminal — must NOT show in --list.
        store.create_task("task_done", scope, "this one finished")
        store.set_result(
            "task_done", json.dumps({"success": True}),
            status=TaskStatus.COMPLETE.value,
        )

        # Cross-tenant leak guard (must not surface in OrgA's scope).
        other = MemoryScope(tenant_id="t2", org_alias="OrgZ")
        store.create_task("task_other_tenant", other, "different tenant")
    return db


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

def test_parse_positional_task_id() -> None:
    from sf_dev_agent.resume_cli import _parse_args
    args = _parse_args(["task_xyz"])
    assert args.task_id == "task_xyz"
    assert args.list is False
    assert args.latest is False


def test_parse_list_flag() -> None:
    from sf_dev_agent.resume_cli import _parse_args
    args = _parse_args(["--list"])
    assert args.list is True
    assert args.task_id is None


def test_parse_latest_flag() -> None:
    from sf_dev_agent.resume_cli import _parse_args
    args = _parse_args(["--latest"])
    assert args.latest is True


def test_parse_mutually_exclusive(capsys: pytest.CaptureFixture[str]) -> None:
    """positional task_id + --list/--latest must reject."""
    from sf_dev_agent.resume_cli import _parse_args
    with pytest.raises(SystemExit):
        _parse_args(["task_x", "--list"])
    with pytest.raises(SystemExit):
        _parse_args(["task_x", "--latest"])


# ---------------------------------------------------------------------------
# --list mode
# ---------------------------------------------------------------------------

def test_list_shows_in_flight_only(
    db_with_tasks: Path, org_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from sf_dev_agent.resume_cli import run_resume_command
    rc = run_resume_command(["--list"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "task_awaiting" in captured.out
    assert "task_old_planning" in captured.out
    # Terminal must NOT appear.
    assert "task_done" not in captured.out
    # Cross-tenant must NOT appear.
    assert "task_other_tenant" not in captured.out


def test_list_with_no_in_flight_prints_friendly_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, org_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = tmp_path / "empty.db"
    monkeypatch.setattr(
        "sf_dev_agent.context.default_db_path",
        lambda: db,
    )
    WorkingMemoryStore(db).close()  # create the schema; no tasks

    from sf_dev_agent.resume_cli import run_resume_command
    rc = run_resume_command(["--list"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "No in-flight tasks" in captured.out


# ---------------------------------------------------------------------------
# --latest resolution
# ---------------------------------------------------------------------------

def test_latest_resolves_to_newest_in_flight(
    db_with_tasks: Path, org_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--latest picks the newest non-terminal task; resume() called with that id."""
    from sf_dev_agent import resume_cli

    captured: dict[str, Any] = {}

    def fake_resume(*, task_id: str, **kwargs: Any) -> Any:
        captured["task_id"] = task_id
        captured["org_alias"] = kwargs["org"].org_alias
        # Return a mock task; resume_cli doesn't inspect the return.
        return None

    monkeypatch.setattr(resume_cli.AgentLoop, "resume", staticmethod(fake_resume))

    rc = resume_cli.run_resume_command(["--latest"])
    assert rc == 0
    assert captured["task_id"] == "task_awaiting"
    assert captured["org_alias"] == "OrgA"


def test_latest_with_no_in_flight_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, org_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = tmp_path / "noinflight.db"
    monkeypatch.setattr(
        "sf_dev_agent.context.default_db_path",
        lambda: db,
    )
    WorkingMemoryStore(db).close()

    from sf_dev_agent.resume_cli import run_resume_command
    rc = run_resume_command(["--latest"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "No in-flight tasks" in captured.out


# ---------------------------------------------------------------------------
# Positional task-id resume
# ---------------------------------------------------------------------------

def test_positional_task_id_dispatches_to_resume(
    db_with_tasks: Path, org_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sf_dev_agent import resume_cli

    captured: dict[str, Any] = {}

    def fake_resume(*, task_id: str, **kwargs: Any) -> Any:
        captured["task_id"] = task_id
        return None

    monkeypatch.setattr(resume_cli.AgentLoop, "resume", staticmethod(fake_resume))

    rc = resume_cli.run_resume_command(["task_awaiting"])
    assert rc == 0
    assert captured["task_id"] == "task_awaiting"


def test_resume_propagates_value_error_from_agent_loop(
    db_with_tasks: Path, org_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A missing or wrong-tenant task surfaces AgentLoop.resume's ValueError."""
    from sf_dev_agent import resume_cli

    def fake_resume(**kwargs: Any) -> Any:
        raise ValueError("task 'never_existed' not found in working memory")

    monkeypatch.setattr(resume_cli.AgentLoop, "resume", staticmethod(fake_resume))

    rc = resume_cli.run_resume_command(["never_existed"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "not found" in captured.out


def test_no_args_errors_with_helpful_message(
    db_with_tasks: Path, org_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from sf_dev_agent.resume_cli import run_resume_command
    rc = run_resume_command([])
    captured = capsys.readouterr()
    assert rc == 1
    assert "task-id" in captured.out or "--list" in captured.out


# ---------------------------------------------------------------------------
# Org config
# ---------------------------------------------------------------------------

def test_no_org_alias_set_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("SF_ORG_ALIAS", raising=False)
    from sf_dev_agent.resume_cli import run_resume_command
    rc = run_resume_command(["--list"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "No Salesforce org configured" in captured.out


def test_org_alias_flag_overrides_env(
    db_with_tasks: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--org-alias` should beat SF_ORG_ALIAS env."""
    monkeypatch.setenv("SF_ORG_ALIAS", "DefaultOrg")
    monkeypatch.setenv("GOOGLE_API_KEY", "AIzaSy-fake")
    monkeypatch.setattr(
        "sf_dev_agent.resume_cli.derive_org_type", lambda _alias: "developer",
    )
    monkeypatch.setattr(
        "sf_dev_agent.resume_cli.derive_instance_url",
        lambda _alias: "https://example.salesforce.com",
    )
    monkeypatch.setattr(
        "sf_dev_agent.resume_cli.derive_api_version", lambda: "62.0",
    )

    from sf_dev_agent import resume_cli

    captured: dict[str, Any] = {}

    def fake_resume(*, task_id: str, **kwargs: Any) -> Any:
        captured["org_alias"] = kwargs["org"].org_alias
        return None

    monkeypatch.setattr(resume_cli.AgentLoop, "resume", staticmethod(fake_resume))

    rc = resume_cli.run_resume_command(["task_awaiting", "--org-alias", "OrgA"])
    assert rc == 0
    assert captured["org_alias"] == "OrgA"


# ---------------------------------------------------------------------------
# Provider error path
# ---------------------------------------------------------------------------

def test_missing_llm_key_errors_after_target_resolved(
    db_with_tasks: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Provider failures fail the resume cleanly, not the target lookup."""
    monkeypatch.setenv("SF_ORG_ALIAS", "OrgA")
    monkeypatch.setenv("SF_ORG_TYPE", "developer")
    monkeypatch.setenv("SF_INSTANCE_URL", "https://example.salesforce.com")
    monkeypatch.setenv("SF_API_VERSION", "62.0")
    # Drop both the explicit provider override AND every API key —
    # without LLM_PROVIDER, the create_provider() no-key branch fires.
    for k in (
        "LLM_PROVIDER", "LLM_MODEL",
        "GOOGLE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(k, raising=False)

    from sf_dev_agent.resume_cli import run_resume_command
    rc = run_resume_command(["--latest"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "Provider error" in captured.out


# ---------------------------------------------------------------------------
# __main__ dispatch
# ---------------------------------------------------------------------------

def test_main_module_dispatches_to_resume(
    db_with_tasks: Path, org_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`python -m sf_dev_agent resume --list` should route to resume_cli."""
    from sf_dev_agent import resume_cli

    called: dict[str, Any] = {}

    def fake_run(argv: list[str]) -> int:
        called["argv"] = argv
        return 0

    monkeypatch.setattr(resume_cli, "run_resume_command", fake_run)
    monkeypatch.setattr(sys, "argv", ["sf-agent", "resume", "--list"])

    from sf_dev_agent.__main__ import main as cli_main
    with pytest.raises(SystemExit) as excinfo:
        cli_main()
    assert excinfo.value.code == 0
    assert called["argv"] == ["--list"]
