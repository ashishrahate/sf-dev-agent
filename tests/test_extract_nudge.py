"""Tests for the end-of-session extract nudge (Phase C.5)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from sf_dev_agent.extract_nudge import (
    extract_skip_path,
    is_extract_skipped,
    mark_extract_skipped,
    prompt_extract_if_needed,
)
from sf_dev_agent.memory import MemoryScope, MemoryStore, WorkingMemoryStore
from sf_dev_agent.memory.extraction import ExtractionResult
from sf_dev_agent.models.schemas import OrgConnection
from sf_dev_agent.providers.base import LLMProvider, LLMResponse
from sf_dev_agent.repl import ReplSession


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class _StubProvider(LLMProvider):
    @property
    def model_name(self) -> str:
        return "stub-1"

    def chat(self, **kwargs: Any) -> LLMResponse:
        return LLMResponse(text_blocks=["stub"], stop_reason="end_turn")


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


@pytest.fixture
def session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    org: OrgConnection, wm: WorkingMemoryStore,
) -> ReplSession:
    """A ReplSession with a tmp DB for default_db_path() lookups."""
    monkeypatch.setattr(
        "sf_dev_agent.context.default_db_path", lambda: tmp_path / "wm.db",
    )
    return ReplSession(
        org=org, provider=_StubProvider(),
        working_memory=wm, mock_org=False,
    )


def _seed_completed_task(
    wm: WorkingMemoryStore,
    org: OrgConnection,
    task_id: str = "t_done",
) -> None:
    scope = MemoryScope(tenant_id=org.tenant_id, org_alias=org.org_alias)
    wm.create_task(
        task_id=task_id, scope=scope,
        user_request="finished work", status="complete",
    )
    wm.append_message(task_id, "user", "finished work")
    wm.append_message(task_id, "assistant", [{"type": "text", "text": "done"}])


# ---------------------------------------------------------------------------
# Skip-sentinel mechanics
# ---------------------------------------------------------------------------

def test_skip_sentinel_round_trip(tmp_path: Path) -> None:
    db = tmp_path / "wm.db"
    assert not is_extract_skipped(db, "tenant1", "OrgA")
    mark_extract_skipped(db, "tenant1", "OrgA")
    assert is_extract_skipped(db, "tenant1", "OrgA")
    # A different (tenant, org) is unaffected.
    assert not is_extract_skipped(db, "tenant1", "OrgB")
    assert not is_extract_skipped(db, "tenant2", "OrgA")


def test_skip_path_sanitizes_org_alias(tmp_path: Path) -> None:
    """org aliases with slashes / colons must produce a safe filename."""
    db = tmp_path / "wm.db"
    p = extract_skip_path(db, "t/x", "Org:slash")
    assert "/" not in p.name
    assert ":" not in p.name


# ---------------------------------------------------------------------------
# prompt_extract_if_needed: short-circuit cases
# ---------------------------------------------------------------------------

def test_prompt_returns_zero_when_no_completed_tasks(
    session: ReplSession,
) -> None:
    assert session.completed_task_ids == []
    assert prompt_extract_if_needed(session) == 0


def test_prompt_returns_zero_when_skipped_sentinel_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    session: ReplSession, wm: WorkingMemoryStore, org: OrgConnection,
) -> None:
    _seed_completed_task(wm, org)
    session.completed_task_ids = ["t_done"]
    mark_extract_skipped(tmp_path / "wm.db", org.tenant_id, org.org_alias)

    # Even if Prompt.ask were called we'd see a test failure, so we
    # deliberately monkeypatch it to a sentinel that would error.
    monkeypatch.setattr(
        "sf_dev_agent.extract_nudge.Prompt.ask",
        lambda *a, **kw: pytest.fail("Prompt.ask should not be reached"),
    )

    assert prompt_extract_if_needed(session) == 0


def test_prompt_returns_zero_when_no_working_memory(
    org: OrgConnection,
) -> None:
    session = ReplSession(
        org=org, provider=_StubProvider(),
        working_memory=None, mock_org=False,
    )
    session.completed_task_ids = ["t_done"]
    assert prompt_extract_if_needed(session) == 0


# ---------------------------------------------------------------------------
# Skip / suppress paths
# ---------------------------------------------------------------------------

def test_prompt_skip_does_not_run_extractor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    session: ReplSession, wm: WorkingMemoryStore, org: OrgConnection,
) -> None:
    _seed_completed_task(wm, org)
    session.completed_task_ids = ["t_done"]

    monkeypatch.setattr(
        "sf_dev_agent.extract_nudge.Prompt.ask", lambda *a, **kw: "skip",
    )

    extractor_calls: list[str] = []
    monkeypatch.setattr(
        "sf_dev_agent.extract_nudge.MemoryExtractor.extract",
        lambda self, task_id: extractor_calls.append(task_id),
    )

    assert prompt_extract_if_needed(session) == 0
    assert extractor_calls == []


def test_prompt_no_and_stop_asking_writes_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    session: ReplSession, wm: WorkingMemoryStore, org: OrgConnection,
) -> None:
    _seed_completed_task(wm, org)
    session.completed_task_ids = ["t_done"]

    monkeypatch.setattr(
        "sf_dev_agent.extract_nudge.Prompt.ask",
        lambda *a, **kw: "no-and-stop-asking",
    )

    assert prompt_extract_if_needed(session) == 0
    assert is_extract_skipped(tmp_path / "wm.db", org.tenant_id, org.org_alias)


# ---------------------------------------------------------------------------
# Yes path: extractor runs, candidates land in MemoryStore
# ---------------------------------------------------------------------------

def test_prompt_yes_path_runs_extractor_and_saves_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    session: ReplSession, wm: WorkingMemoryStore, org: OrgConnection,
) -> None:
    _seed_completed_task(wm, org)
    session.completed_task_ids = ["t_done"]

    # A reply chain for Prompt.ask: first call is the soft-prompt (yes),
    # subsequent calls are the candidate save prompts (alternate yes/no).
    answers = iter(["yes", "yes", "no"])
    monkeypatch.setattr(
        "sf_dev_agent.extract_nudge.Prompt.ask",
        lambda *a, **kw: next(answers),
    )

    from sf_dev_agent.memory.extraction import ExtractedMemoryCandidate

    fake_result = ExtractionResult(
        task_id="t_done",
        candidates=[
            ExtractedMemoryCandidate(
                type="feedback", name="prefers-bundled-prs",
                description="user likes one PR per refactor",
                body="rule X. **Why:** session evidence. **How to apply:** future refactors.",
                confidence=0.9,
                evidence_quote="bundled was the right call",
            ),
            ExtractedMemoryCandidate(
                type="user", name="role",
                description="user is a senior eng",
                body="senior; long Salesforce background.",
                confidence=0.7,
                evidence_quote="ten years on the platform",
            ),
        ],
    )

    monkeypatch.setattr(
        "sf_dev_agent.extract_nudge.MemoryExtractor.extract",
        lambda self, task_id: fake_result,
    )

    saved = prompt_extract_if_needed(session)

    assert saved == 1, f"expected only the first candidate accepted, got {saved}"

    # Confirm the saved memory landed in MemoryStore under the right scope.
    db = tmp_path / "wm.db"
    with MemoryStore(db) as ms:
        rows = ms.list(
            scope=MemoryScope(tenant_id=org.tenant_id, org_alias=org.org_alias),
        )
    names = [r.name for r in rows]
    assert "prefers-bundled-prs" in names
    assert "role" not in names


def test_prompt_yes_with_extractor_parse_error_does_not_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    session: ReplSession, wm: WorkingMemoryStore, org: OrgConnection,
) -> None:
    _seed_completed_task(wm, org)
    session.completed_task_ids = ["t_done"]

    monkeypatch.setattr(
        "sf_dev_agent.extract_nudge.Prompt.ask", lambda *a, **kw: "yes",
    )
    monkeypatch.setattr(
        "sf_dev_agent.extract_nudge.MemoryExtractor.extract",
        lambda self, task_id: ExtractionResult(
            task_id=task_id, parse_error="bogus JSON",
        ),
    )

    assert prompt_extract_if_needed(session) == 0


def test_prompt_yes_with_no_candidates_returns_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    session: ReplSession, wm: WorkingMemoryStore, org: OrgConnection,
) -> None:
    _seed_completed_task(wm, org)
    session.completed_task_ids = ["t_done"]

    monkeypatch.setattr(
        "sf_dev_agent.extract_nudge.Prompt.ask", lambda *a, **kw: "yes",
    )
    monkeypatch.setattr(
        "sf_dev_agent.extract_nudge.MemoryExtractor.extract",
        lambda self, task_id: ExtractionResult(
            task_id=task_id, candidates=[], skipped_low_confidence=2,
        ),
    )

    assert prompt_extract_if_needed(session) == 0
