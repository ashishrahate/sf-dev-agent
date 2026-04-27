"""Unit tests for MemoryExtractor (Wave 8 slice 3a).

No live LLM calls — uses a scripted FakeProvider that returns whatever
JSON we want as the model's response.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sf_dev_agent.memory import MemoryScope, WorkingMemoryStore
from sf_dev_agent.memory.extraction import (
    ExtractedMemoryCandidate,
    MemoryExtractor,
    _parse_json_block,
)
from sf_dev_agent.providers.base import LLMProvider, LLMResponse

# ---------------------------------------------------------------------------
# Fake provider — replays scripted text_blocks
# ---------------------------------------------------------------------------

class _FakeProvider(LLMProvider):
    def __init__(self, text: str) -> None:
        self._text = text
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
        return LLMResponse(text_blocks=[self._text], stop_reason="end_turn")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store_with_task(tmp_path: Path) -> WorkingMemoryStore:
    """A WorkingMemoryStore with one task that has a small transcript."""
    db = tmp_path / "wm.db"
    store = WorkingMemoryStore(db)
    scope = MemoryScope(tenant_id="t1", org_alias="OrgA")
    store.create_task("task_extract_test", scope, "Build a dedup trigger")
    store.append_message(
        "task_extract_test", "user",
        "Build a dedup trigger for Account.",
    )
    store.append_message(
        "task_extract_test", "assistant",
        [{"type": "text", "text": "I'll match on email and phone."}],
    )
    store.append_message(
        "task_extract_test", "user",
        "Yes, prefer email + phone matching — that's the right call here.",
    )
    yield store
    store.close()


def _candidate_json(*candidates: dict[str, Any]) -> str:
    return json.dumps({"candidates": list(candidates)})


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_extract_returns_validated_candidates(
    store_with_task: WorkingMemoryStore,
) -> None:
    provider = _FakeProvider(_candidate_json(
        {
            "type": "feedback",
            "name": "dedup-email-phone",
            "description": "user confirmed email+phone match for account dedup",
            "body": (
                "Rule: match Email__c + Phone.\n"
                "**Why:** user explicitly endorsed.\n"
                "**How to apply:** any account dedup logic."
            ),
            "confidence": 0.9,
            "evidence_quote": "Yes, prefer email + phone matching — that's the right call here.",
        },
    ))
    extractor = MemoryExtractor(working_memory=store_with_task, provider=provider)
    result = extractor.extract("task_extract_test")

    assert result.parse_error is None
    assert len(result.candidates) == 1
    cand = result.candidates[0]
    assert isinstance(cand, ExtractedMemoryCandidate)
    assert cand.type == "feedback"
    assert cand.name == "dedup-email-phone"
    assert cand.confidence == 0.9


def test_extract_includes_transcript_in_provider_call(
    store_with_task: WorkingMemoryStore,
) -> None:
    """The provider must see both the user request and the message bodies."""
    provider = _FakeProvider(_candidate_json())
    extractor = MemoryExtractor(working_memory=store_with_task, provider=provider)
    extractor.extract("task_extract_test")

    assert provider.calls, "provider.chat must be called"
    transcript = provider.calls[0][0]["content"]
    assert "Build a dedup trigger for Account" in transcript
    assert "match on email and phone" in transcript
    assert "Yes, prefer email + phone matching" in transcript


# ---------------------------------------------------------------------------
# Confidence threshold + top-K
# ---------------------------------------------------------------------------

def test_extract_drops_low_confidence_candidates(
    store_with_task: WorkingMemoryStore,
) -> None:
    provider = _FakeProvider(_candidate_json(
        {
            "type": "feedback", "name": "high",
            "description": "x", "body": "x",
            "confidence": 0.9, "evidence_quote": "y",
        },
        {
            "type": "feedback", "name": "low",
            "description": "x", "body": "x",
            "confidence": 0.3, "evidence_quote": "y",
        },
    ))
    extractor = MemoryExtractor(
        working_memory=store_with_task, provider=provider,
        min_confidence=0.6,
    )
    result = extractor.extract("task_extract_test")

    names = {c.name for c in result.candidates}
    assert names == {"high"}
    assert result.skipped_low_confidence == 1


def test_extract_caps_at_max_candidates_keeping_highest_confidence(
    store_with_task: WorkingMemoryStore,
) -> None:
    provider = _FakeProvider(_candidate_json(
        *[
            {
                "type": "user", "name": f"c{i}",
                "description": "x", "body": "x",
                "confidence": 0.7 + i * 0.05,
                "evidence_quote": "y",
            }
            for i in range(6)  # six candidates, all above threshold
        ]
    ))
    extractor = MemoryExtractor(
        working_memory=store_with_task, provider=provider,
        max_candidates=3,
    )
    result = extractor.extract("task_extract_test")

    assert len(result.candidates) == 3
    # Sorted by confidence descending — keeps the strongest three.
    confidences = [c.confidence for c in result.candidates]
    assert confidences == sorted(confidences, reverse=True)
    assert result.candidates[0].name == "c5"  # highest confidence
    assert result.skipped_low_confidence == 3


# ---------------------------------------------------------------------------
# Defensive parsing
# ---------------------------------------------------------------------------

def test_extract_handles_fenced_json(
    store_with_task: WorkingMemoryStore,
) -> None:
    """LLMs sometimes wrap JSON in ```json fences — parser must tolerate it."""
    provider = _FakeProvider(
        "Here are the candidates:\n```json\n"
        + _candidate_json({
            "type": "user", "name": "role",
            "description": "x", "body": "user is staff eng",
            "confidence": 0.8, "evidence_quote": "y",
        })
        + "\n```\nLet me know if you want changes."
    )
    extractor = MemoryExtractor(working_memory=store_with_task, provider=provider)
    result = extractor.extract("task_extract_test")
    assert result.parse_error is None
    assert len(result.candidates) == 1


def test_extract_returns_parse_error_on_garbage(
    store_with_task: WorkingMemoryStore,
) -> None:
    provider = _FakeProvider("definitely not JSON anywhere in here")
    extractor = MemoryExtractor(working_memory=store_with_task, provider=provider)
    result = extractor.extract("task_extract_test")

    assert result.parse_error is not None
    assert result.candidates == []


def test_extract_empty_candidates_list(
    store_with_task: WorkingMemoryStore,
) -> None:
    provider = _FakeProvider(_candidate_json())
    extractor = MemoryExtractor(working_memory=store_with_task, provider=provider)
    result = extractor.extract("task_extract_test")
    assert result.candidates == []
    assert result.parse_error is None


def test_extract_skips_candidates_with_bad_shape(
    store_with_task: WorkingMemoryStore,
) -> None:
    """Malformed candidates (wrong type, missing fields) are silently dropped."""
    provider = _FakeProvider(json.dumps({"candidates": [
        {"type": "lore", "name": "x", "description": "x", "body": "x", "confidence": 0.9},
        {"type": "user", "name": "", "description": "x", "body": "x", "confidence": 0.9},
        {"type": "user", "name": "ok", "description": "x", "body": "x", "confidence": 0.9},
        {"type": "user", "name": "neg", "description": "x", "body": "x", "confidence": -0.1},
    ]}))
    extractor = MemoryExtractor(working_memory=store_with_task, provider=provider)
    result = extractor.extract("task_extract_test")
    assert {c.name for c in result.candidates} == {"ok"}


# ---------------------------------------------------------------------------
# Provider failures
# ---------------------------------------------------------------------------

def test_extract_provider_exception_returns_parse_error(
    store_with_task: WorkingMemoryStore,
) -> None:
    class _BoomProvider(LLMProvider):
        @property
        def model_name(self) -> str:
            return "boom"

        def chat(self, **kwargs: Any) -> LLMResponse:
            raise RuntimeError("simulated API failure")

    extractor = MemoryExtractor(
        working_memory=store_with_task, provider=_BoomProvider(),
    )
    result = extractor.extract("task_extract_test")
    assert result.parse_error is not None
    assert "simulated API failure" in result.parse_error
    assert result.candidates == []


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------

def test_extract_unknown_task_raises(
    store_with_task: WorkingMemoryStore,
) -> None:
    provider = _FakeProvider(_candidate_json())
    extractor = MemoryExtractor(working_memory=store_with_task, provider=provider)
    with pytest.raises(ValueError, match="not found"):
        extractor.extract("never_existed")


def test_extract_empty_transcript_raises(
    tmp_path: Path,
) -> None:
    db = tmp_path / "empty.db"
    store = WorkingMemoryStore(db)
    scope = MemoryScope(tenant_id="t1", org_alias="OrgA")
    store.create_task("task_empty", scope, "x")
    # Don't append any messages.

    provider = _FakeProvider(_candidate_json())
    extractor = MemoryExtractor(working_memory=store, provider=provider)
    with pytest.raises(ValueError, match="no conversation"):
        extractor.extract("task_empty")
    store.close()


def test_extractor_rejects_invalid_thresholds(
    store_with_task: WorkingMemoryStore,
) -> None:
    provider = _FakeProvider(_candidate_json())
    with pytest.raises(ValueError):
        MemoryExtractor(
            working_memory=store_with_task, provider=provider,
            min_confidence=1.5,
        )
    with pytest.raises(ValueError):
        MemoryExtractor(
            working_memory=store_with_task, provider=provider,
            max_candidates=0,
        )


# ---------------------------------------------------------------------------
# JSON block parser unit tests
# ---------------------------------------------------------------------------

def test_parse_json_block_plain() -> None:
    assert _parse_json_block('{"a": 1}') == {"a": 1}


def test_parse_json_block_fenced() -> None:
    assert _parse_json_block('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_json_block_embedded() -> None:
    text = 'Here is the data: {"a": 1, "b": [2, 3]} — that is all.'
    assert _parse_json_block(text) == {"a": 1, "b": [2, 3]}


def test_parse_json_block_empty_raises() -> None:
    with pytest.raises(ValueError):
        _parse_json_block("")


def test_parse_json_block_no_json_raises() -> None:
    with pytest.raises(ValueError):
        _parse_json_block("no braces anywhere")
