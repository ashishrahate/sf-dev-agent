"""LLM call audit store — Item 2 token-usage audit per tool.

Writes one row per LLM call into the `llm_invocations` table so audit
queries can answer:

  - "Per-task: how many tokens did each turn use?"
  - "Across tasks: which tools cost the most input tokens?"
  - "Are prompt-cache hits actually firing on Anthropic?"

The store shares the same SQLite file as the metadata index + working
memory (`default_db_path()`), so a single DB open per session serves
every audit/memory layer. Writes are best-effort: a SQLite hiccup logs
but never breaks the agent loop.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sf_dev_agent.providers.base import TokenUsage

logger = logging.getLogger(__name__)


@dataclass
class LLMInvocationRecord:
    """One persisted LLM call. Mirrors the `llm_invocations` row schema."""
    tenant_id: str
    task_id: str
    turn_idx: int
    provider: str
    model: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    org_alias: str | None = None
    triggered_by_tool: str | None = None
    emitted_tools: list[str] = field(default_factory=list)
    stop_reason: str | None = None
    started_at: str = ""
    duration_ms: int = 0
    mode: str | None = None


@dataclass
class ToolAggregate:
    """One row of the `--by tool` aggregation view."""
    tool_name: str | None
    calls: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class ModelAggregate:
    """One row of the `--by model` aggregation view."""
    provider: str
    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int


class LLMAuditStore:
    """SQLite-backed audit log for LLM calls.

    Opens against the same DB file as MetadataIndex / WorkingMemoryStore.
    The schema is shared via `context/schema.sql`; calling `_ensure_schema`
    is safe across all stores because every CREATE is IF NOT EXISTS.
    """

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        schema_path = (
            Path(__file__).resolve().parent / "context" / "schema.sql"
        )
        self._conn.executescript(schema_path.read_text(encoding="utf-8"))

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> LLMAuditStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def record(self, rec: LLMInvocationRecord) -> int:
        """Persist one LLM call. Returns the inserted row id.

        Caller controls all fields except `started_at` — if it's empty,
        we stamp the current UTC time. Defensive defaults on `usage` and
        `emitted_tools` keep aggregation arithmetic clean when a provider
        didn't report usage.
        """
        started = rec.started_at or _now_iso()
        cursor = self._conn.execute(
            """
            INSERT INTO llm_invocations (
                tenant_id, org_alias, task_id, turn_idx, provider, model,
                input_tokens, output_tokens,
                cache_read_tokens, cache_write_tokens,
                triggered_by_tool, emitted_tools_json,
                stop_reason, started_at, duration_ms, mode
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rec.tenant_id, rec.org_alias, rec.task_id, rec.turn_idx,
                rec.provider, rec.model,
                rec.usage.input_tokens, rec.usage.output_tokens,
                rec.usage.cache_read_tokens, rec.usage.cache_write_tokens,
                rec.triggered_by_tool,
                json.dumps(list(rec.emitted_tools)),
                rec.stop_reason, started, rec.duration_ms, rec.mode,
            ),
        )
        self._conn.commit()
        return int(cursor.lastrowid or 0)

    # ------------------------------------------------------------------
    # Read views — the surfaces the CLI / REPL render
    # ------------------------------------------------------------------

    def list_for_task(self, task_id: str) -> list[LLMInvocationRecord]:
        """All invocations for one task, in turn order."""
        rows = self._conn.execute(
            "SELECT * FROM llm_invocations "
            "WHERE task_id = ? ORDER BY turn_idx ASC, id ASC",
            (task_id,),
        ).fetchall()
        return [_row_to_record(r) for r in rows]

    def aggregate_by_tool(
        self,
        *,
        tenant_id: str | None = None,
        org_alias: str | None = None,
        since: str | None = None,
        include_untriggered: bool = True,
    ) -> list[ToolAggregate]:
        """Sum usage grouped by `triggered_by_tool`.

        - `tenant_id` / `org_alias` filter to a single scope (None = all).
        - `since` is an ISO-8601 timestamp lower bound on `started_at`.
        - `include_untriggered=False` drops first-turn calls (no parent tool).
        Returned rows are sorted descending by total tokens — the headline
        "what costs the most" view.
        """
        where, params = _build_scope_filter(tenant_id, org_alias, since)
        sql = (
            "SELECT triggered_by_tool, COUNT(*) AS calls, "
            "       SUM(input_tokens)       AS input_tokens, "
            "       SUM(output_tokens)      AS output_tokens, "
            "       SUM(cache_read_tokens)  AS cache_read_tokens, "
            "       SUM(cache_write_tokens) AS cache_write_tokens "
            "FROM llm_invocations "
            f"{where} "
            "GROUP BY triggered_by_tool "
            "ORDER BY (input_tokens + output_tokens) DESC"
        )
        rows = self._conn.execute(sql, params).fetchall()
        out: list[ToolAggregate] = []
        for r in rows:
            if not include_untriggered and r["triggered_by_tool"] is None:
                continue
            out.append(ToolAggregate(
                tool_name=r["triggered_by_tool"],
                calls=int(r["calls"] or 0),
                input_tokens=int(r["input_tokens"] or 0),
                output_tokens=int(r["output_tokens"] or 0),
                cache_read_tokens=int(r["cache_read_tokens"] or 0),
                cache_write_tokens=int(r["cache_write_tokens"] or 0),
            ))
        return out

    def aggregate_by_model(
        self,
        *,
        tenant_id: str | None = None,
        org_alias: str | None = None,
        since: str | None = None,
    ) -> list[ModelAggregate]:
        """Sum usage grouped by (provider, model)."""
        where, params = _build_scope_filter(tenant_id, org_alias, since)
        sql = (
            "SELECT provider, model, COUNT(*) AS calls, "
            "       SUM(input_tokens)       AS input_tokens, "
            "       SUM(output_tokens)      AS output_tokens, "
            "       SUM(cache_read_tokens)  AS cache_read_tokens, "
            "       SUM(cache_write_tokens) AS cache_write_tokens "
            "FROM llm_invocations "
            f"{where} "
            "GROUP BY provider, model "
            "ORDER BY (input_tokens + output_tokens) DESC"
        )
        rows = self._conn.execute(sql, params).fetchall()
        return [
            ModelAggregate(
                provider=r["provider"], model=r["model"],
                calls=int(r["calls"] or 0),
                input_tokens=int(r["input_tokens"] or 0),
                output_tokens=int(r["output_tokens"] or 0),
                cache_read_tokens=int(r["cache_read_tokens"] or 0),
                cache_write_tokens=int(r["cache_write_tokens"] or 0),
            )
            for r in rows
        ]

    def summary(
        self,
        *,
        tenant_id: str | None = None,
        org_alias: str | None = None,
        since: str | None = None,
    ) -> dict[str, int]:
        """Single-row total. Keys: calls, input_tokens, output_tokens,
        cache_read_tokens, cache_write_tokens.
        """
        where, params = _build_scope_filter(tenant_id, org_alias, since)
        sql = (
            "SELECT COUNT(*) AS calls, "
            "       COALESCE(SUM(input_tokens), 0)       AS input_tokens, "
            "       COALESCE(SUM(output_tokens), 0)      AS output_tokens, "
            "       COALESCE(SUM(cache_read_tokens), 0)  AS cache_read_tokens, "
            "       COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens "
            f"FROM llm_invocations {where}"
        )
        row = self._conn.execute(sql, params).fetchone()
        return {
            "calls": int(row["calls"] or 0),
            "input_tokens": int(row["input_tokens"] or 0),
            "output_tokens": int(row["output_tokens"] or 0),
            "cache_read_tokens": int(row["cache_read_tokens"] or 0),
            "cache_write_tokens": int(row["cache_write_tokens"] or 0),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _build_scope_filter(
    tenant_id: str | None,
    org_alias: str | None,
    since: str | None,
) -> tuple[str, tuple]:
    """Compose the WHERE clause + params for the aggregation queries.

    Returns ("WHERE …", params) or ("", ()) when no filters are set.
    Each filter is appended only when its argument is non-None — bare
    None still matches rows where the column is NULL.
    """
    clauses: list[str] = []
    params: list = []
    if tenant_id is not None:
        clauses.append("tenant_id = ?")
        params.append(tenant_id)
    if org_alias is not None:
        clauses.append("org_alias = ?")
        params.append(org_alias)
    if since is not None:
        clauses.append("started_at >= ?")
        params.append(since)
    if not clauses:
        return "", ()
    return "WHERE " + " AND ".join(clauses), tuple(params)


def _row_to_record(row: sqlite3.Row) -> LLMInvocationRecord:
    """Hydrate a `llm_invocations` row back into the dataclass shape."""
    try:
        emitted: list[str] = list(json.loads(row["emitted_tools_json"] or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        emitted = []
    return LLMInvocationRecord(
        tenant_id=row["tenant_id"],
        task_id=row["task_id"],
        turn_idx=int(row["turn_idx"] or 0),
        provider=row["provider"],
        model=row["model"],
        usage=TokenUsage(
            input_tokens=int(row["input_tokens"] or 0),
            output_tokens=int(row["output_tokens"] or 0),
            cache_read_tokens=int(row["cache_read_tokens"] or 0),
            cache_write_tokens=int(row["cache_write_tokens"] or 0),
        ),
        org_alias=row["org_alias"],
        triggered_by_tool=row["triggered_by_tool"],
        emitted_tools=emitted,
        stop_reason=row["stop_reason"],
        started_at=row["started_at"],
        duration_ms=int(row["duration_ms"] or 0),
        mode=row["mode"],
    )


__all__ = [
    "LLMAuditStore",
    "LLMInvocationRecord",
    "ModelAggregate",
    "ToolAggregate",
]
