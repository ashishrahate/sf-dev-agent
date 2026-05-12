"""Working-memory store — task state + conversation transcript persistence.

Wave 8 slice 2a. Lives in the same SQLite file as `MetadataIndex`,
`KnowledgeBase`, and `MemoryStore` (`default_db_path()`), so a single
DB serves every memory tier the agent uses.

Why this exists:
    - **Resume from crash.** A killed process can come back and pick up the
      same task by re-loading the conversation transcript and the task row.
    - **Audit trail.** Every tool call the agent made is preserved in the
      assistant message blocks; later we can diff "what the user asked for"
      against "what the agent actually did".
    - **Slice 2c input.** LLM-driven memory extraction needs the full
      transcript to scan for save-worthy moments.

Public API:
    WorkingMemoryStore(db_path)
        .create_task(task_id, scope, user_request, status="planning") -> TaskRow
        .update_task_status(task_id, status, error=None)
        .set_plan(task_id, plan_json)
        .set_plan_approved(task_id, approved=True)
        .set_result(task_id, result_json, status, completed_at=None)
        .get_task(task_id) -> TaskRow | None
        .list_tasks(scope, status=None, limit=50) -> list[TaskRow]
        .append_message(task_id, role, content) -> int  # returns new seq
        .load_messages(task_id) -> list[dict]
        .delete_task(task_id) -> bool                   # cascades to messages
        .stats(scope=None) -> dict[status -> count]

Content serialization:
    Message `content` is whatever the agent's chat loop produces: usually a
    string for user messages, or a list of typed blocks (text, tool_use,
    tool_result) for assistant + tool-result messages. We JSON-encode it
    transparently — `load_messages` returns the same shape that was stored.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sf_dev_agent.memory.store import MemoryScope

logger = logging.getLogger(__name__)


# Statuses that mean the task is no longer running. `set_result` and
# `update_task_status` use this set to decide whether to stamp `completed_at`.
TERMINAL_STATUSES: frozenset[str] = frozenset({
    "complete", "failed", "rolled_back",
})


@dataclass
class TaskRow:
    """A row in the `tasks` table, hydrated for callers."""
    id: str
    tenant_id: str
    org_alias: str | None
    status: str
    user_request: str
    plan_json: str | None
    plan_approved: bool
    result_json: str | None
    error: str | None
    created_at: str
    updated_at: str
    completed_at: str | None
    mode: str = "plan"  # AgentMode enum value (slice C); default for older DBs
    pending_question: str | None = None  # slice 4: question text while AWAITING_USER_INPUT


class WorkingMemoryStore:
    """SQLite-backed task + conversation persistence."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        # Ensures `tasks` and `conversation_messages` exist for DBs that
        # pre-date slice 2a.
        schema_path = (
            Path(__file__).resolve().parent.parent / "context" / "schema.sql"
        )
        self._conn.executescript(schema_path.read_text(encoding="utf-8"))
        self._migrate_add_mode_column()
        self._migrate_add_pending_question_column()

    def _migrate_add_mode_column(self) -> None:
        """Slice C migration: add `mode` to existing `tasks` tables.

        `CREATE TABLE IF NOT EXISTS` is no-op when the table already
        exists, so the new column doesn't land on pre-slice-C DBs via
        the schema script. Run an ALTER TABLE that's idempotent —
        SQLite raises `OperationalError: duplicate column name` once
        the column is in place; we swallow that specific error and
        let any other (real schema problem) bubble up.
        """
        try:
            self._conn.execute(
                "ALTER TABLE tasks ADD COLUMN mode TEXT NOT NULL DEFAULT 'plan'"
            )
            self._conn.commit()
        except sqlite3.OperationalError as exc:
            if "duplicate column name" in str(exc).lower():
                return
            raise

    def _migrate_add_pending_question_column(self) -> None:
        """Slice 4 migration: add `pending_question` for AWAITING_USER_INPUT.

        Same idempotent ALTER pattern as the mode column above.
        """
        try:
            self._conn.execute(
                "ALTER TABLE tasks ADD COLUMN pending_question TEXT"
            )
            self._conn.commit()
        except sqlite3.OperationalError as exc:
            if "duplicate column name" in str(exc).lower():
                return
            raise

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> WorkingMemoryStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Task lifecycle
    # ------------------------------------------------------------------

    def create_task(
        self,
        task_id: str,
        scope: MemoryScope,
        user_request: str,
        status: str = "planning",
        mode: str = "plan",
    ) -> TaskRow:
        """Insert a new tasks row. Idempotent on PK collision (returns existing)."""
        if not task_id:
            raise ValueError("task_id is required")
        if not scope.tenant_id:
            raise ValueError("scope.tenant_id is required")
        if not user_request.strip():
            raise ValueError("user_request is required")

        existing = self.get_task(task_id)
        if existing is not None:
            return existing

        now = _now_iso()
        self._conn.execute(
            """
            INSERT INTO tasks (
                id, tenant_id, org_alias, status, user_request,
                plan_approved, mode, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                task_id, scope.tenant_id, scope.org_alias, status,
                user_request, mode, now, now,
            ),
        )
        self._conn.commit()
        row = self.get_task(task_id)
        assert row is not None  # we just inserted it
        return row

    def update_task_status(
        self,
        task_id: str,
        status: str,
        error: str | None = None,
    ) -> None:
        """Set status; stamp `completed_at` on terminal transitions."""
        now = _now_iso()
        if status in TERMINAL_STATUSES:
            self._conn.execute(
                """
                UPDATE tasks
                SET status = ?, error = COALESCE(?, error),
                    updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (status, error, now, now, task_id),
            )
        else:
            self._conn.execute(
                """
                UPDATE tasks
                SET status = ?, error = COALESCE(?, error), updated_at = ?
                WHERE id = ?
                """,
                (status, error, now, task_id),
            )
        self._conn.commit()

    def set_plan(self, task_id: str, plan_json: str) -> None:
        """Persist the serialized ExecutionPlan when the agent registers one."""
        self._conn.execute(
            "UPDATE tasks SET plan_json = ?, updated_at = ? WHERE id = ?",
            (plan_json, _now_iso(), task_id),
        )
        self._conn.commit()

    def set_plan_approved(self, task_id: str, approved: bool = True) -> None:
        self._conn.execute(
            "UPDATE tasks SET plan_approved = ?, updated_at = ? WHERE id = ?",
            (1 if approved else 0, _now_iso(), task_id),
        )
        self._conn.commit()

    def set_pending_question(
        self, task_id: str, question: str | None,
    ) -> None:
        """Slice 4: persist the prompt the LLM is awaiting an answer to.

        Pass `None` to clear. Used by the `request_user_input` tool
        handler and by the answer-fed resume path.
        """
        self._conn.execute(
            "UPDATE tasks SET pending_question = ?, updated_at = ? WHERE id = ?",
            (question, _now_iso(), task_id),
        )
        self._conn.commit()

    def set_result(
        self,
        task_id: str,
        result_json: str,
        status: str,
        error: str | None = None,
    ) -> None:
        """Stamp the final outcome. Always treated as a terminal transition."""
        now = _now_iso()
        self._conn.execute(
            """
            UPDATE tasks
            SET result_json = ?, status = ?, error = COALESCE(?, error),
                updated_at = ?,
                completed_at = COALESCE(completed_at, ?)
            WHERE id = ?
            """,
            (result_json, status, error, now, now, task_id),
        )
        self._conn.commit()

    def get_task(self, task_id: str) -> TaskRow | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return _row_to_task(row) if row else None

    def list_tasks(
        self,
        scope: MemoryScope,
        status: str | None = None,
        limit: int = 50,
    ) -> list[TaskRow]:
        """List tasks in scope, newest first.

        Scope semantics match `MemoryStore`: tenant strict; `org_alias`
        matches the requested org OR `NULL` (cross-org tasks like the
        agent's own setup work).
        """
        clauses = ["tenant_id = ?"]
        params: list = [scope.tenant_id]
        if scope.org_alias is None:
            clauses.append("org_alias IS NULL")
        else:
            clauses.append("(org_alias = ? OR org_alias IS NULL)")
            params.append(scope.org_alias)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)

        # Tiebreaker on `id DESC` for tasks created in the same second —
        # without it, list_tasks ordering becomes implementation-defined and
        # things like resume --latest get flaky in tests + fast scripts.
        sql = (
            f"SELECT * FROM tasks WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at DESC, id DESC LIMIT ?"
        )
        rows = self._conn.execute(sql, (*params, limit)).fetchall()
        return [_row_to_task(r) for r in rows]

    def delete_task(self, task_id: str) -> bool:
        """Hard-delete a task and (via FK CASCADE) its conversation."""
        cur = self._conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        self._conn.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Conversation transcript
    # ------------------------------------------------------------------

    def append_message(
        self,
        task_id: str,
        role: str,
        content: Any,
    ) -> int:
        """Append a message to the transcript. Returns the assigned seq.

        `content` can be a string (typical user message) or a list of typed
        blocks (assistant or tool-results). It's JSON-serialized as-is.
        """
        if role not in ("user", "assistant"):
            raise ValueError(f"unknown role {role!r}; must be 'user' or 'assistant'")

        # next-seq is best-effort — we lock by task_id at the row level via
        # the UNIQUE (task_id, seq) constraint. A retry would only happen
        # under concurrent appends, which the agent doesn't do.
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), -1) AS max_seq "
            "FROM conversation_messages WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        # `or` would mis-treat 0 as missing — use explicit None check.
        max_seq = row["max_seq"] if row["max_seq"] is not None else -1
        next_seq = max_seq + 1

        self._conn.execute(
            """
            INSERT INTO conversation_messages (
                task_id, seq, role, content_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (task_id, next_seq, role, json.dumps(content), _now_iso()),
        )
        # Bump the task's updated_at so list_tasks ordering stays fresh.
        self._conn.execute(
            "UPDATE tasks SET updated_at = ? WHERE id = ?",
            (_now_iso(), task_id),
        )
        self._conn.commit()
        return next_seq

    def load_messages(self, task_id: str) -> list[dict[str, Any]]:
        """Return the conversation in seq order, with content deserialized."""
        rows = self._conn.execute(
            "SELECT role, content_json FROM conversation_messages "
            "WHERE task_id = ? ORDER BY seq ASC",
            (task_id,),
        ).fetchall()
        return [
            {"role": r["role"], "content": json.loads(r["content_json"])}
            for r in rows
        ]

    def message_count(self, task_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM conversation_messages WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return int(row["n"] or 0)

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self, scope: MemoryScope | None = None) -> dict[str, int]:
        """Per-status counts. With `scope`, restrict to that tenant+org."""
        if scope is None:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
            ).fetchall()
        else:
            clauses = ["tenant_id = ?"]
            params: list = [scope.tenant_id]
            if scope.org_alias is None:
                clauses.append("org_alias IS NULL")
            else:
                clauses.append("(org_alias = ? OR org_alias IS NULL)")
                params.append(scope.org_alias)
            sql = (
                f"SELECT status, COUNT(*) AS n FROM tasks "
                f"WHERE {' AND '.join(clauses)} GROUP BY status"
            )
            rows = self._conn.execute(sql, params).fetchall()
        return {r["status"]: r["n"] for r in rows}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _row_to_task(row: sqlite3.Row) -> TaskRow:
    # Defensive default — `mode` / `pending_question` may be missing on
    # rows from very old DBs that somehow skipped a migration.
    keys = row.keys() if hasattr(row, "keys") else None
    mode_val = row["mode"] if keys is None or "mode" in keys else "plan"
    pending = (
        row["pending_question"]
        if keys is None or "pending_question" in keys else None
    )
    return TaskRow(
        id=row["id"],
        tenant_id=row["tenant_id"],
        org_alias=row["org_alias"],
        status=row["status"],
        user_request=row["user_request"],
        plan_json=row["plan_json"],
        plan_approved=bool(row["plan_approved"]),
        result_json=row["result_json"],
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        completed_at=row["completed_at"],
        mode=mode_val or "plan",
        pending_question=pending,
    )
