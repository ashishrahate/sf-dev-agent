"""LLM-driven end-of-session memory extraction.

Wave 8 slice 3a. Reads a persisted conversation transcript, asks the
configured LLM provider to identify save-worthy moments, and proposes
typed memory candidates with confidence scores + evidence quotes.

Why this exists:
    Manual `memory_save` calls catch the obvious cases (the user said
    "remember that ..."), but the harder save trigger from Claude Code's
    auto-memory is the *quiet confirmation* — a user accepting a non-
    obvious choice without pushing back. An end-of-session sweep, with
    the full transcript in hand, is the right shape to catch those.

Design notes:
    - **Manual gate.** Extraction proposes candidates; persistence happens
      only after the human accepts (the CLI wraps this). The class itself
      doesn't write — it returns a `ExtractionResult` for the caller.
    - **JSON-shaped output.** The provider is asked for strict JSON; we
      parse defensively (LLMs occasionally wrap in code fences or chatter).
    - **Confidence threshold.** Low-confidence candidates are dropped
      before the user even sees them — keeps approval fatigue down.
    - **Evidence quotes.** Each candidate carries a `evidence_quote` from
      the transcript. The user can verify the LLM didn't hallucinate.

Public API:
    MemoryExtractor(working_memory, provider,
                    min_confidence=0.6, max_candidates=5)
        .extract(task_id) -> ExtractionResult

    ExtractedMemoryCandidate(type, name, description, body,
                              confidence, evidence_quote)

    ExtractionResult(task_id, candidates, skipped_low_confidence,
                     parse_error=None)
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sf_dev_agent.memory.store import MEMORY_TYPES

if TYPE_CHECKING:
    from sf_dev_agent.memory.working import WorkingMemoryStore
    from sf_dev_agent.providers.base import LLMProvider

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ExtractedMemoryCandidate:
    """One memory the LLM proposes saving from a session transcript."""
    type: str
    name: str
    description: str
    body: str
    confidence: float
    evidence_quote: str


@dataclass
class ExtractionResult:
    task_id: str
    candidates: list[ExtractedMemoryCandidate] = field(default_factory=list)
    skipped_low_confidence: int = 0
    parse_error: str | None = None
    raw_response: str = ""


# ---------------------------------------------------------------------------
# Extraction prompt
# ---------------------------------------------------------------------------

_EXTRACTION_SYSTEM_PROMPT = """You are a memory-extraction assistant.

You will be given the full conversation transcript of an agent session.
Your job is to identify durable facts that should be saved across sessions
so a future agent run can recall them. You DO NOT save anything yourself —
you only propose candidates; a human reviews each one before it lands.

Use exactly this four-type taxonomy:
  - "user":      facts about the human user (role, preferences, knowledge)
  - "feedback":  corrections AND validated non-obvious choices
  - "project":   ongoing work, decisions, deadlines, named constraints
  - "reference": pointers to external systems (Linear projects, Grafana
                 dashboards, Slack channels, etc.) and what they're for

When to propose a candidate:
  - The user CORRECTED the agent's approach (save as "feedback").
  - The user CONFIRMED a non-obvious agent choice without pushback (save
    as "feedback" — these are easy to miss but matter).
  - The user named a constraint, deadline, decision, stakeholder, or
    external system that future sessions should know about.
  - The user shared something durable about themselves or how they work.

When NOT to propose a candidate:
  - The fact is already in the codebase (file paths, function names,
    architecture). Future agents can read the code.
  - The fact is ephemeral debugging context.
  - The fact would surface from `git log` / `git blame`.
  - The transcript only implies it — extract only what is EXPLICITLY in
    the conversation. No inference. No extrapolation.

Body convention for "feedback" and "project" candidates:
  rule statement
  **Why:** the reason cited in the transcript (often a past incident or
  a strong stated preference)
  **How to apply:** when/where this guidance kicks in

For "user" and "reference" types, body can be a single short paragraph.

Output format — strict JSON, no prose, no code fences:
{
  "candidates": [
    {
      "type": "user|feedback|project|reference",
      "name": "kebab-case-handle",
      "description": "one-line relevance hook",
      "body": "the memory content (rule + Why + How to apply)",
      "confidence": 0.0,
      "evidence_quote": "the line from the transcript that justifies this"
    }
  ]
}

confidence is your subjective certainty in [0.0, 1.0] that this candidate
is worth saving. 1.0 = explicit, durable, no-doubt; 0.5 = plausible but
inferred; below 0.5 = don't propose.

If the transcript contains nothing save-worthy, return:
{"candidates": []}

Propose at most 5 candidates per session. Quality over quantity."""


# ---------------------------------------------------------------------------
# MemoryExtractor
# ---------------------------------------------------------------------------

class MemoryExtractor:
    """Runs end-of-session extraction against a persisted transcript."""

    def __init__(
        self,
        working_memory: WorkingMemoryStore,
        provider: LLMProvider,
        *,
        min_confidence: float = 0.6,
        max_candidates: int = 5,
    ) -> None:
        if not 0.0 <= min_confidence <= 1.0:
            raise ValueError("min_confidence must be in [0, 1]")
        if max_candidates < 1:
            raise ValueError("max_candidates must be >= 1")
        self._wm = working_memory
        self._provider = provider
        self._min_confidence = min_confidence
        self._max_candidates = max_candidates

    def extract(self, task_id: str) -> ExtractionResult:
        """Load the transcript for `task_id` and propose memory candidates.

        Returns an ExtractionResult even when extraction fails or the LLM
        produces unparseable output — `parse_error` carries the diagnostic.
        Raises only on missing task or empty transcript.
        """
        task = self._wm.get_task(task_id)
        if task is None:
            raise ValueError(f"task {task_id!r} not found in working memory")

        messages = self._wm.load_messages(task_id)
        if not messages:
            raise ValueError(
                f"task {task_id!r} has no conversation messages — nothing to extract"
            )

        transcript = _render_transcript(task.user_request, messages)
        return self._extract_from_transcript(task_id, transcript)

    def _extract_from_transcript(
        self, task_id: str, transcript: str
    ) -> ExtractionResult:
        try:
            response = self._provider.chat(
                system=_EXTRACTION_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": transcript}],
                tools=[],
            )
        except Exception as exc:
            logger.exception("Extraction LLM call failed for task %s", task_id)
            return ExtractionResult(
                task_id=task_id,
                parse_error=f"{type(exc).__name__}: {exc}",
            )

        raw_text = "\n".join(response.text_blocks).strip()
        result = ExtractionResult(task_id=task_id, raw_response=raw_text)

        try:
            parsed = _parse_json_block(raw_text)
        except ValueError as exc:
            result.parse_error = f"JSON parse failed: {exc}"
            logger.warning("Extraction JSON parse failed: %s", exc)
            return result

        candidates_raw = parsed.get("candidates") or []
        if not isinstance(candidates_raw, list):
            result.parse_error = "'candidates' must be a JSON array"
            return result

        candidates: list[ExtractedMemoryCandidate] = []
        skipped = 0

        for item in candidates_raw:
            if not isinstance(item, dict):
                continue
            cand = _coerce_candidate(item)
            if cand is None:
                continue
            if cand.confidence < self._min_confidence:
                skipped += 1
                continue
            candidates.append(cand)

        # Top-K by confidence so the user sees only the strongest signals.
        candidates.sort(key=lambda c: c.confidence, reverse=True)
        if len(candidates) > self._max_candidates:
            skipped += len(candidates) - self._max_candidates
            candidates = candidates[: self._max_candidates]

        result.candidates = candidates
        result.skipped_low_confidence = skipped
        return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _render_transcript(user_request: str, messages: list[dict[str, Any]]) -> str:
    """Flatten a list of message dicts into a single human-readable transcript.

    Tool-use and tool-result blocks are rendered compactly — the LLM doesn't
    need the full JSON of every tool call to extract memories, just the
    surrounding human/agent dialogue.
    """
    lines: list[str] = [f"# Original request\n{user_request}\n", "# Transcript"]
    for msg in messages:
        role = msg.get("role", "?")
        content = msg.get("content", "")
        if isinstance(content, str):
            lines.append(f"\n## {role}\n{content}")
            continue
        # Block-style content (assistant or tool_results).
        rendered_blocks: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                rendered_blocks.append(str(block))
                continue
            btype = block.get("type", "?")
            if btype == "text":
                rendered_blocks.append(block.get("text", ""))
            elif btype == "tool_use":
                tool = block.get("name", "?")
                rendered_blocks.append(f"[tool_call: {tool}]")
            elif btype == "tool_result":
                # The actual tool output is usually noise; just record the
                # fact that a tool ran. Length-cap so giant payloads don't
                # blow the prompt.
                body = str(block.get("content", ""))
                if len(body) > 200:
                    body = body[:200] + "…"
                rendered_blocks.append(f"[tool_result: {body}]")
            else:
                rendered_blocks.append(f"[{btype}]")
        lines.append(f"\n## {role}\n" + "\n".join(rendered_blocks))
    return "\n".join(lines)


_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?```", re.DOTALL)


def _parse_json_block(text: str) -> dict[str, Any]:
    """Parse JSON from the LLM's response, tolerant of code fences + chatter.

    Strategy:
      1. Try the whole response as JSON.
      2. If that fails, look for a fenced code block and try its body.
      3. If that fails, find the first balanced { ... } and try that.
    """
    if not text:
        raise ValueError("empty response")

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    fenced = _FENCED_JSON_RE.search(text)
    if fenced:
        body = fenced.group(1).strip()
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            pass

    # First balanced top-level object as a last resort.
    start = text.find("{")
    if start >= 0:
        depth = 0
        for i in range(start, len(text)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break

    raise ValueError("could not locate a parseable JSON object in the response")


def _coerce_candidate(item: dict[str, Any]) -> ExtractedMemoryCandidate | None:
    """Validate + normalize one raw JSON candidate. Returns None on bad shape."""
    type_ = item.get("type")
    name = item.get("name")
    description = item.get("description")
    body = item.get("body")
    confidence = item.get("confidence")
    evidence = item.get("evidence_quote", "")

    if type_ not in MEMORY_TYPES:
        return None
    if not isinstance(name, str) or not name.strip():
        return None
    if not isinstance(description, str) or not description.strip():
        return None
    if not isinstance(body, str) or not body.strip():
        return None
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        return None
    if not 0.0 <= confidence <= 1.0:
        return None
    if not isinstance(evidence, str):
        evidence = ""

    return ExtractedMemoryCandidate(
        type=type_,
        name=name.strip(),
        description=description.strip(),
        body=body.strip(),
        confidence=confidence,
        evidence_quote=evidence.strip(),
    )
