"""SQLite-backed memory store — project memory for the agent.

Mirrors the patterns in `MetadataIndex` and `KnowledgeBase`: BLOB embeddings,
hash-gated re-embedding, cosine-sim search via numpy. Lives in the same
SQLite file (`default_db_path()`) so a single open of the DB serves all four
context layers.

Type taxonomy (ported from Claude Code's auto-memory):
    user       -> who the human is, role, preferences, knowledge
    feedback   -> corrections AND validated non-obvious choices
    project    -> ongoing work, decisions, deadlines
    reference  -> pointers to external systems (dashboards, ticket projects)

Scope: every row carries `tenant_id` + `org_alias`. Recall filters strictly
by tenant; org_alias is "match this org or NULL (cross-org)" so a memory
can be marked global within a tenant.

Public API:
    MemoryStore(db_path)
        .save(record) -> MemoryRecord
        .recall(query_embedding, scope, type=None, limit=10) -> list[MemoryRecallHit]
        .list(scope, type=None, limit=50) -> list[MemoryRecord]
        .find_by_id(id) -> MemoryRecord | None
        .supersede(old_id, new_id) -> None
        .embed_pending(embedder, force=False) -> MemoryEmbedResult
        .stats(scope) -> dict
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from sf_dev_agent.context.embedders.base import Embedder, hash_text

logger = logging.getLogger(__name__)


# Locked taxonomy. Tools and the orchestrator validate against this set —
# extending the taxonomy is a deliberate, cross-component change.
MEMORY_TYPES: frozenset[str] = frozenset({"user", "feedback", "project", "reference"})


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------

@dataclass
class MemoryScope:
    """Where a memory belongs. Tenant is required; org is optional.

    A NULL `org_alias` row applies to every org under the tenant (e.g. user
    preferences). Recall always filters by tenant_id and treats org_alias
    as: "match the requested org OR rows where org_alias IS NULL".
    """
    tenant_id: str
    org_alias: str | None = None


@dataclass
class MemoryRecord:
    """A row in the `memories` table, hydrated for callers."""
    id: str
    tenant_id: str
    org_alias: str | None
    type: str
    name: str
    description: str
    body: str
    tags: list[str]
    source_session_id: str | None
    created_at: str
    last_accessed_at: str
    access_count: int
    superseded_by: str | None


@dataclass
class MemoryRecallHit:
    record: MemoryRecord
    score: float


@dataclass
class MemoryEmbedResult:
    embedded: int = 0
    skipped_unchanged: int = 0
    errors: list[str] = field(default_factory=list)
    embedder_name: str = ""


# ---------------------------------------------------------------------------
# Slug + id helpers
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str) -> str:
    """Turn a name into a stable, filename-safe slug."""
    s = _SLUG_RE.sub("-", text.lower()).strip("-")
    return s or "memory"


def make_memory_id(scope: MemoryScope, name: str) -> str:
    """Compose a stable id `<tenant>:<org_or_global>:<slug>:<short-hash>`.

    The short hash off the (scope, name) tuple disambiguates same-named
    memories within one scope — without it, two `save()` calls with the same
    `name` would collide. The hash is deterministic so callers can re-derive
    the id if needed.
    """
    org = scope.org_alias or "global"
    slug = _slugify(name)
    digest = hashlib.sha256(
        f"{scope.tenant_id}|{org}|{slug}".encode()
    ).hexdigest()[:8]
    return f"{scope.tenant_id}:{org}:{slug}:{digest}"


# ---------------------------------------------------------------------------
# MemoryStore
# ---------------------------------------------------------------------------

class MemoryStore:
    """Read/write interface to the SQLite `memories` table."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        # Ensures the `memories` table exists for DBs that pre-date Wave 8.
        schema_path = (
            Path(__file__).resolve().parent.parent / "context" / "schema.sql"
        )
        self._conn.executescript(schema_path.read_text(encoding="utf-8"))

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> MemoryStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def save(
        self,
        scope: MemoryScope,
        type: str,
        name: str,
        description: str,
        body: str,
        tags: Iterable[str] | None = None,
        source_session_id: str | None = None,
        memory_id: str | None = None,
    ) -> MemoryRecord:
        """Upsert a memory row. Re-saving the same id replaces body/desc/tags.

        On update, `created_at` and `access_count` are preserved;
        `last_accessed_at` is bumped to "now".

        Embeddings are NOT generated here — call `embed_pending()` to refresh
        them in batch. This mirrors the metadata-index / knowledge-base split
        between ingestion and embedding.
        """
        if type not in MEMORY_TYPES:
            raise ValueError(
                f"unknown memory type {type!r}; must be one of {sorted(MEMORY_TYPES)}"
            )
        if not scope.tenant_id:
            raise ValueError("scope.tenant_id is required")
        if not name.strip():
            raise ValueError("name is required")
        if not body.strip():
            raise ValueError("body is required")

        record_id = memory_id or make_memory_id(scope, name)
        tag_list = list(tags or [])
        tags_json = json.dumps(tag_list)
        now = _now_iso()

        existing = self._conn.execute(
            "SELECT created_at, access_count FROM memories WHERE id = ?",
            (record_id,),
        ).fetchone()

        if existing is None:
            self._conn.execute(
                """
                INSERT INTO memories (
                    id, tenant_id, org_alias, type, name, description, body,
                    tags_json, source_session_id, created_at, last_accessed_at,
                    access_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    record_id, scope.tenant_id, scope.org_alias, type,
                    name, description, body, tags_json, source_session_id,
                    now, now,
                ),
            )
        else:
            # Body changed -> the embedding is stale. Drop it so the next
            # `embed_pending()` rebuilds the vector.
            self._conn.execute(
                """
                UPDATE memories
                SET tenant_id = ?, org_alias = ?, type = ?, name = ?,
                    description = ?, body = ?, tags_json = ?,
                    source_session_id = COALESCE(?, source_session_id),
                    last_accessed_at = ?,
                    embedding = NULL, embedded_text_hash = NULL
                WHERE id = ?
                """,
                (
                    scope.tenant_id, scope.org_alias, type, name,
                    description, body, tags_json, source_session_id,
                    now, record_id,
                ),
            )

        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM memories WHERE id = ?", (record_id,)
        ).fetchone()
        return _row_to_record(row)

    def supersede(self, old_id: str, new_id: str) -> None:
        """Link `old_id -> new_id`. Used by compaction (slice 2)."""
        self._conn.execute(
            "UPDATE memories SET superseded_by = ? WHERE id = ?",
            (new_id, old_id),
        )
        self._conn.commit()

    def delete(self, memory_id: str) -> bool:
        """Hard-delete a row. Generally avoid — superseded_by is the soft path."""
        cur = self._conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        self._conn.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Embeddings
    # ------------------------------------------------------------------

    @staticmethod
    def _embedding_text(row: sqlite3.Row) -> str:
        """Compose the canonical text we embed for a memory.

        Type + name + description up front gives strong recall signal;
        the body provides the fine-grained content. Same shape as the
        knowledge-base `_embedding_text`.
        """
        tags = " ".join(json.loads(row["tags_json"] or "[]"))
        return (
            f"{row['type']} {tags}\n"
            f"{row['name']}\n"
            f"{row['description']}\n\n"
            f"{row['body']}"
        )

    def embed_pending(
        self,
        embedder: Embedder,
        force: bool = False,
        batch_size: int = 32,
    ) -> MemoryEmbedResult:
        """Hash-gated embed/refresh — same pattern as KnowledgeBase.embed_entries."""
        result = MemoryEmbedResult(embedder_name=embedder.name)

        rows = self._conn.execute("SELECT * FROM memories").fetchall()

        to_embed: list[tuple[str, str, str]] = []
        for row in rows:
            text = self._embedding_text(row)
            current_hash = hash_text(text)
            stored_hash = row["embedded_text_hash"]
            if not force and stored_hash == current_hash and row["embedding"] is not None:
                result.skipped_unchanged += 1
                continue
            to_embed.append((row["id"], text, current_hash))

        if not to_embed:
            return result

        for start in range(0, len(to_embed), batch_size):
            chunk = to_embed[start:start + batch_size]
            texts = [t for (_, t, _) in chunk]
            try:
                vectors = embedder.embed(texts)
            except Exception as exc:
                result.errors.append(f"batch {start}: {type(exc).__name__}: {exc}")
                logger.error("Memory embedding batch failed: %s", exc)
                continue

            for (memory_id, _, content_hash), vec in zip(chunk, vectors):
                blob = np.asarray(vec, dtype=np.float32).tobytes()
                self._conn.execute(
                    """
                    UPDATE memories
                    SET embedding = ?, embedded_text_hash = ?
                    WHERE id = ?
                    """,
                    (blob, content_hash, memory_id),
                )
                result.embedded += 1

        self._conn.commit()
        return result

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def find_by_id(self, memory_id: str) -> MemoryRecord | None:
        row = self._conn.execute(
            "SELECT * FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        return _row_to_record(row) if row else None

    def list(
        self,
        scope: MemoryScope,
        type: str | None = None,
        include_superseded: bool = False,
        limit: int = 50,
    ) -> list[MemoryRecord]:
        """List memories in a scope, newest first."""
        sql, params = _scope_clause(scope, type=type, include_superseded=include_superseded)
        rows = self._conn.execute(
            f"SELECT * FROM memories WHERE {sql} ORDER BY created_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [_row_to_record(r) for r in rows]

    def recall(
        self,
        query_embedding: np.ndarray,
        scope: MemoryScope,
        type: str | None = None,
        limit: int = 10,
        include_superseded: bool = False,
    ) -> list[MemoryRecallHit]:
        """Cosine-sim ranking over scope-matching memories with an embedding.

        Side effect: bumps `last_accessed_at` and `access_count` on every row
        returned. This feeds the decay scoring planned for slice 2.
        """
        query = np.asarray(query_embedding, dtype=np.float32)
        norm = np.linalg.norm(query)
        if norm > 0:
            query = query / norm

        sql, params = _scope_clause(scope, type=type, include_superseded=include_superseded)
        rows = self._conn.execute(
            f"SELECT * FROM memories WHERE {sql} AND embedding IS NOT NULL",
            params,
        ).fetchall()

        if not rows:
            return []

        matrix = np.vstack([
            np.frombuffer(r["embedding"], dtype=np.float32) for r in rows
        ])
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        matrix = matrix / norms

        scores = matrix @ query

        if len(scores) <= limit:
            top_idx = np.argsort(-scores)
        else:
            partition = np.argpartition(-scores, limit)[:limit]
            top_idx = partition[np.argsort(-scores[partition])]

        hits = [
            MemoryRecallHit(
                record=_row_to_record(rows[int(i)]),
                score=float(scores[int(i)]),
            )
            for i in top_idx
        ]

        # Bump access counters on the returned rows.
        if hits:
            now = _now_iso()
            self._conn.executemany(
                "UPDATE memories SET last_accessed_at = ?, "
                "access_count = access_count + 1 WHERE id = ?",
                [(now, h.record.id) for h in hits],
            )
            self._conn.commit()

        return hits

    def stats(self, scope: MemoryScope | None = None) -> dict[str, int]:
        """Per-type counts. With `scope`, restrict to that tenant+org."""
        if scope is None:
            rows = self._conn.execute(
                "SELECT type, COUNT(*) AS n FROM memories GROUP BY type"
            ).fetchall()
        else:
            sql, params = _scope_clause(scope, include_superseded=True)
            rows = self._conn.execute(
                f"SELECT type, COUNT(*) AS n FROM memories WHERE {sql} GROUP BY type",
                params,
            ).fetchall()
        return {r["type"]: r["n"] for r in rows}

    def embedding_stats(self, scope: MemoryScope | None = None) -> dict[str, int]:
        """Embedding coverage; mirrors KnowledgeBase.embedding_stats shape."""
        if scope is None:
            row = self._conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN embedding IS NOT NULL THEN 1 ELSE 0 END) AS embedded
                FROM memories
                """
            ).fetchone()
        else:
            sql, params = _scope_clause(scope, include_superseded=True)
            row = self._conn.execute(
                f"""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN embedding IS NOT NULL THEN 1 ELSE 0 END) AS embedded
                FROM memories WHERE {sql}
                """,
                params,
            ).fetchone()
        return {"total": row["total"] or 0, "embedded": row["embedded"] or 0}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _scope_clause(
    scope: MemoryScope,
    type: str | None = None,
    include_superseded: bool = False,
) -> tuple[str, tuple]:
    """Build the WHERE fragment + params for a scoped query.

    Always filters tenant_id strictly. For `org_alias`, returns rows where
    `org_alias = scope.org_alias` OR `org_alias IS NULL` (the "cross-org
    within tenant" case). Excludes superseded rows by default.
    """
    clauses = ["tenant_id = ?"]
    params: list = [scope.tenant_id]

    if scope.org_alias is None:
        clauses.append("org_alias IS NULL")
    else:
        clauses.append("(org_alias = ? OR org_alias IS NULL)")
        params.append(scope.org_alias)

    if type is not None:
        if type not in MEMORY_TYPES:
            raise ValueError(
                f"unknown memory type {type!r}; must be one of {sorted(MEMORY_TYPES)}"
            )
        clauses.append("type = ?")
        params.append(type)

    if not include_superseded:
        clauses.append("superseded_by IS NULL")

    return " AND ".join(clauses), tuple(params)


def _row_to_record(row: sqlite3.Row) -> MemoryRecord:
    return MemoryRecord(
        id=row["id"],
        tenant_id=row["tenant_id"],
        org_alias=row["org_alias"],
        type=row["type"],
        name=row["name"],
        description=row["description"],
        body=row["body"],
        tags=json.loads(row["tags_json"] or "[]"),
        source_session_id=row["source_session_id"],
        created_at=row["created_at"],
        last_accessed_at=row["last_accessed_at"],
        access_count=row["access_count"],
        superseded_by=row["superseded_by"],
    )
