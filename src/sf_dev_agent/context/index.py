"""SQLite-backed metadata index.

The schema is intentionally generic (`components` + `relationships` tables with
JSON metadata columns) so that adding a new metadata type — ValidationRule,
Flow, CustomMetadataType, etc. — never requires DDL changes.

Public API:
    MetadataIndex(db_path).
    .upsert_component(component)
    .upsert_relationship(rel)         # silently skipped if target_id missing
    .find_by_id(id) / find_by_name(api_name) / find_by_type(component_type)
    .triggers_on(object_api_name)
    .fields_of(object_api_name)
    .search(text)                     # LIKE-based for now; vector search later
    .stats()                          # counts per component_type
    .start_run(...) / finish_run(...) # records ingestion run metadata
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import numpy as np

from sf_dev_agent.context.embedders.base import Embedder, hash_text
from sf_dev_agent.context.parsers.base import ParsedComponent, ParsedRelationship

logger = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


@dataclass
class ComponentRow:
    """A row from the `components` table, hydrated for callers."""
    id: str
    component_type: str
    api_name: str
    parent_id: str | None
    file_path: str | None
    source: str | None
    metadata: dict[str, Any]
    last_indexed_at: str


@dataclass
class RelationshipEdge:
    """A graph edge with the partner component hydrated for the caller's perspective.

    `direction` is "outgoing" when the queried component is the source of the edge
    (e.g. AccountTrigger TRIGGERS_ON Account), "incoming" when it's the target
    (e.g. Account is referenced BY AccountTrigger).
    """
    direction: str                 # "outgoing" | "incoming"
    relationship_type: str
    partner: ComponentRow
    metadata: dict[str, Any]


@dataclass
class SemanticSearchHit:
    """A hit from semantic_search — component plus cosine similarity score."""
    component: ComponentRow
    score: float                   # cosine similarity in [-1, 1]; for normalized vectors -> [0, 1]


@dataclass
class EmbeddingRefreshResult:
    """Outcome of an embed_components run."""
    embedded: int = 0              # newly-computed embeddings written this run
    skipped_unchanged: int = 0     # source hash matched -> no re-embed needed
    skipped_no_source: int = 0     # rows with no source text (e.g. malformed)
    errors: list[str] = field(default_factory=list)
    embedder_name: str = ""


class MetadataIndex:
    """Read/write interface to the SQLite metadata index."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        self._migrate()

    def _migrate(self) -> None:
        """Idempotent column migrations for DBs created before a column existed.

        SQLite has no `ADD COLUMN IF NOT EXISTS`, so we attempt each ALTER and
        swallow the duplicate-column error. Cheap, correct, and never destroys data.

        Also re-runs the schema script so newly-introduced tables (e.g.
        knowledge_entries added in slice 3) appear on existing DBs without any
        manual migration step from the user.
        """
        for stmt in (
            "ALTER TABLE components ADD COLUMN embedding BLOB",
            "ALTER TABLE components ADD COLUMN embedded_source_hash TEXT",
        ):
            try:
                self._conn.execute(stmt)
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "MetadataIndex":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def upsert_component(self, component: ParsedComponent) -> None:
        """Insert or replace one component row."""
        self._conn.execute(
            """
            INSERT INTO components (
                id, component_type, api_name, parent_id,
                file_path, source, metadata_json, last_indexed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                component_type  = excluded.component_type,
                api_name        = excluded.api_name,
                parent_id       = excluded.parent_id,
                file_path       = excluded.file_path,
                source          = excluded.source,
                metadata_json   = excluded.metadata_json,
                last_indexed_at = excluded.last_indexed_at
            """,
            (
                component.id,
                component.component_type,
                component.api_name,
                component.parent_id,
                component.file_path,
                component.source,
                json.dumps(component.metadata, default=str),
                _now_iso(),
            ),
        )

    def upsert_relationship(self, rel: ParsedRelationship) -> bool:
        """Insert one relationship edge. Returns False if either endpoint isn't in the index.

        Skipping dangling edges is deliberate — a trigger may reference a standard
        sObject (Account, Contact) that we never ingested. The relationship is
        kept silent rather than raising; the trigger's `metadata.target_object`
        field still records it.
        """
        if not (self._exists(rel.source_id) and self._exists(rel.target_id)):
            return False
        self._conn.execute(
            """
            INSERT OR IGNORE INTO relationships (
                source_id, target_id, relationship_type, metadata_json
            ) VALUES (?, ?, ?, ?)
            """,
            (rel.source_id, rel.target_id, rel.relationship_type,
             json.dumps(rel.metadata, default=str)),
        )
        return True

    def commit(self) -> None:
        self._conn.commit()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        try:
            yield
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def find_by_id(self, component_id: str) -> ComponentRow | None:
        row = self._conn.execute(
            "SELECT * FROM components WHERE id = ?", (component_id,)
        ).fetchone()
        return _row_to_component(row) if row else None

    def find_by_name(
        self, api_name: str, component_type: str | None = None
    ) -> list[ComponentRow]:
        if component_type:
            rows = self._conn.execute(
                "SELECT * FROM components WHERE api_name = ? AND component_type = ?",
                (api_name, component_type),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM components WHERE api_name = ?", (api_name,)
            ).fetchall()
        return [_row_to_component(r) for r in rows]

    def find_by_type(self, component_type: str) -> list[ComponentRow]:
        rows = self._conn.execute(
            "SELECT * FROM components WHERE component_type = ? ORDER BY api_name",
            (component_type,),
        ).fetchall()
        return [_row_to_component(r) for r in rows]

    def triggers_on(self, object_api_name: str) -> list[ComponentRow]:
        """Return every ApexTrigger registered as TRIGGERS_ON the given object.

        Filtered to ApexTrigger specifically — record-triggered Flows also
        emit a TRIGGERS_ON edge, but for those use the generic relationship
        API (or a future `flows_on` accessor).
        """
        target_id = f"CustomObject:{object_api_name}"
        rows = self._conn.execute(
            """
            SELECT c.* FROM components c
            JOIN relationships r ON r.source_id = c.id
            WHERE r.target_id = ?
              AND r.relationship_type = 'TRIGGERS_ON'
              AND c.component_type = 'ApexTrigger'
            ORDER BY c.api_name
            """,
            (target_id,),
        ).fetchall()
        return [_row_to_component(r) for r in rows]

    def fields_of(self, object_api_name: str) -> list[ComponentRow]:
        """Return every CustomField parented to the given object."""
        parent_id = f"CustomObject:{object_api_name}"
        rows = self._conn.execute(
            """
            SELECT * FROM components
            WHERE parent_id = ? AND component_type = 'CustomField'
            ORDER BY api_name
            """,
            (parent_id,),
        ).fetchall()
        return [_row_to_component(r) for r in rows]

    def search(
        self, text: str, component_type: str | None = None, limit: int = 50
    ) -> list[ComponentRow]:
        """LIKE-based substring search over api_name + source.

        Lightweight — vector search slots in here later as a different backend.
        """
        like = f"%{text}%"
        if component_type:
            rows = self._conn.execute(
                """
                SELECT * FROM components
                WHERE component_type = ?
                  AND (api_name LIKE ? OR source LIKE ?)
                ORDER BY api_name
                LIMIT ?
                """,
                (component_type, like, like, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT * FROM components
                WHERE api_name LIKE ? OR source LIKE ?
                ORDER BY component_type, api_name
                LIMIT ?
                """,
                (like, like, limit),
            ).fetchall()
        return [_row_to_component(r) for r in rows]

    def relationships_of(
        self,
        component_id: str,
        direction: str = "both",
    ) -> list[RelationshipEdge]:
        """Return graph edges touching `component_id`.

        direction = "outgoing" -> only edges where component is the source
        direction = "incoming" -> only edges where component is the target
        direction = "both"     -> both, with `RelationshipEdge.direction` set
        """
        edges: list[RelationshipEdge] = []

        if direction in ("outgoing", "both"):
            rows = self._conn.execute(
                """
                SELECT r.relationship_type, r.metadata_json, c.*
                FROM relationships r
                JOIN components c ON c.id = r.target_id
                WHERE r.source_id = ?
                ORDER BY r.relationship_type, c.api_name
                """,
                (component_id,),
            ).fetchall()
            for row in rows:
                edges.append(RelationshipEdge(
                    direction="outgoing",
                    relationship_type=row["relationship_type"],
                    partner=_row_to_component(row),
                    metadata=json.loads(row["metadata_json"] or "{}"),
                ))

        if direction in ("incoming", "both"):
            rows = self._conn.execute(
                """
                SELECT r.relationship_type, r.metadata_json, c.*
                FROM relationships r
                JOIN components c ON c.id = r.source_id
                WHERE r.target_id = ?
                ORDER BY r.relationship_type, c.api_name
                """,
                (component_id,),
            ).fetchall()
            for row in rows:
                edges.append(RelationshipEdge(
                    direction="incoming",
                    relationship_type=row["relationship_type"],
                    partner=_row_to_component(row),
                    metadata=json.loads(row["metadata_json"] or "{}"),
                ))

        return edges

    def stats(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT component_type, COUNT(*) AS n FROM components GROUP BY component_type"
        ).fetchall()
        return {r["component_type"]: r["n"] for r in rows}

    # ------------------------------------------------------------------
    # Delta-refresh support: who's already indexed, and bulk delete
    # ------------------------------------------------------------------

    def inventory_for_types(self, component_types: list[str]) -> dict[str, str]:
        """Return {component_id: last_indexed_at} for rows of the given types.

        Backs the delta planner — pair this with the org's Tooling-API
        inventory and compare timestamps.
        """
        if not component_types:
            return {}
        placeholders = ",".join(["?"] * len(component_types))
        rows = self._conn.execute(
            f"""
            SELECT id, last_indexed_at FROM components
            WHERE component_type IN ({placeholders})
            """,
            tuple(component_types),
        ).fetchall()
        return {r["id"]: r["last_indexed_at"] for r in rows}

    def delete_components(self, component_ids: list[str]) -> int:
        """Bulk-delete components by id. CASCADE drops their relationships.

        Returns the number of rows deleted.
        """
        if not component_ids:
            return 0
        placeholders = ",".join(["?"] * len(component_ids))
        cur = self._conn.execute(
            f"DELETE FROM components WHERE id IN ({placeholders})",
            tuple(component_ids),
        )
        self._conn.commit()
        return cur.rowcount or 0

    def delete_children_of(self, parent_ids: list[str]) -> int:
        """Delete every row whose parent_id is in `parent_ids`.

        Used by delta refresh before re-ingesting a parent (e.g. CustomObject):
        clears stale children whose source files no longer exist in the org so
        the post-ingest state matches the org's exactly. The parent rows
        themselves are untouched — the caller's targeted retrieve + ingest
        will upsert them with current source.
        """
        if not parent_ids:
            return 0
        placeholders = ",".join(["?"] * len(parent_ids))
        cur = self._conn.execute(
            f"DELETE FROM components WHERE parent_id IN ({placeholders})",
            tuple(parent_ids),
        )
        self._conn.commit()
        return cur.rowcount or 0

    # ------------------------------------------------------------------
    # Embeddings
    # ------------------------------------------------------------------

    @staticmethod
    def _embedding_text(component: ComponentRow | sqlite3.Row) -> str:
        """Compose the canonical text we embed for a component.

        Including type + name biases the embedding toward both name-similarity
        (so "AccountHandler" still finds "AccountTriggerHandler") AND
        body-similarity (so "duplicate detection" finds the relevant code).
        """
        ctype = component["component_type"] if isinstance(component, sqlite3.Row) else component.component_type
        name = component["api_name"] if isinstance(component, sqlite3.Row) else component.api_name
        src = component["source"] if isinstance(component, sqlite3.Row) else component.source
        body = src or ""
        return f"{ctype} {name}\n{body}"

    def embed_components(
        self,
        embedder: Embedder,
        component_types: list[str] | None = None,
        batch_size: int = 32,
        force: bool = False,
    ) -> EmbeddingRefreshResult:
        """Populate or refresh embeddings for components in the index.

        Hash-gated: if `embedded_source_hash` matches the current source's hash,
        the row is skipped. Pass `force=True` to embed everything regardless
        (useful after switching embedder models — different dim / different
        ranking, so old vectors aren't comparable to new query embeddings).
        """
        result = EmbeddingRefreshResult(embedder_name=embedder.name)

        # Pull every row that needs work. We compare the current source's hash
        # against the stored embedded_source_hash to decide.
        if component_types:
            placeholders = ",".join(["?"] * len(component_types))
            rows = self._conn.execute(
                f"SELECT * FROM components WHERE component_type IN ({placeholders})",
                tuple(component_types),
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM components").fetchall()

        # Collect what to embed.
        to_embed: list[tuple[str, str, str]] = []  # (component_id, text, hash)
        for row in rows:
            text = self._embedding_text(row)
            if not (row["source"] or "").strip():
                result.skipped_no_source += 1
                continue
            current_hash = hash_text(text)
            stored_hash = row["embedded_source_hash"]
            if not force and stored_hash == current_hash and row["embedding"] is not None:
                result.skipped_unchanged += 1
                continue
            to_embed.append((row["id"], text, current_hash))

        if not to_embed:
            return result

        # Batch — most providers accept an N-text payload; we still loop in
        # batch_size chunks so a single huge org doesn't trigger payload limits.
        for start in range(0, len(to_embed), batch_size):
            chunk = to_embed[start:start + batch_size]
            texts = [t for (_, t, _) in chunk]
            try:
                vectors = embedder.embed(texts)
            except Exception as exc:
                result.errors.append(f"batch starting at {start}: {type(exc).__name__}: {exc}")
                logger.error("Embedding batch failed: %s", exc)
                continue

            with self.transaction():
                for (component_id, _, content_hash), vec in zip(chunk, vectors):
                    blob = np.asarray(vec, dtype=np.float32).tobytes()
                    self._conn.execute(
                        """
                        UPDATE components
                        SET embedding = ?, embedded_source_hash = ?
                        WHERE id = ?
                        """,
                        (blob, content_hash, component_id),
                    )
                    result.embedded += 1

        return result

    def clear_embeddings(self, component_types: list[str] | None = None) -> int:
        """Wipe stored embeddings + their hashes. Returns rows cleared."""
        if component_types:
            placeholders = ",".join(["?"] * len(component_types))
            cur = self._conn.execute(
                f"""
                UPDATE components
                SET embedding = NULL, embedded_source_hash = NULL
                WHERE component_type IN ({placeholders})
                """,
                tuple(component_types),
            )
        else:
            cur = self._conn.execute(
                "UPDATE components SET embedding = NULL, embedded_source_hash = NULL"
            )
        self._conn.commit()
        return cur.rowcount or 0

    def embedding_stats(self) -> dict[str, dict[str, int]]:
        """Return per-type embedding coverage: {type: {total, embedded}}."""
        rows = self._conn.execute(
            """
            SELECT
                component_type,
                COUNT(*) AS total,
                SUM(CASE WHEN embedding IS NOT NULL THEN 1 ELSE 0 END) AS embedded
            FROM components
            GROUP BY component_type
            """
        ).fetchall()
        return {
            r["component_type"]: {"total": r["total"], "embedded": r["embedded"]}
            for r in rows
        }

    def semantic_search(
        self,
        query_embedding: np.ndarray,
        component_type: str | None = None,
        limit: int = 10,
    ) -> list[SemanticSearchHit]:
        """Return the top-k components ranked by cosine similarity to the query.

        For normalized vectors (which both MockEmbedder and GeminiEmbedder
        produce), cosine similarity = dot product. We do the math in numpy
        across all embedded rows; SQLite is just key-value storage here.
        """
        query = np.asarray(query_embedding, dtype=np.float32)
        norm = np.linalg.norm(query)
        if norm > 0:
            query = query / norm

        if component_type:
            rows = self._conn.execute(
                """
                SELECT * FROM components
                WHERE embedding IS NOT NULL AND component_type = ?
                """,
                (component_type,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM components WHERE embedding IS NOT NULL"
            ).fetchall()

        if not rows:
            return []

        # Stack all candidate vectors, batch dot-product.
        matrix = np.vstack([
            np.frombuffer(r["embedding"], dtype=np.float32) for r in rows
        ])
        # Defensive normalize — if a stored vector wasn't normalized at write time.
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        matrix = matrix / norms

        scores = matrix @ query  # shape: (N,)

        # Take top-k indices, descending.
        if len(scores) <= limit:
            top_idx = np.argsort(-scores)
        else:
            # argpartition is O(N); slice + sort the small head.
            partition = np.argpartition(-scores, limit)[:limit]
            top_idx = partition[np.argsort(-scores[partition])]

        return [
            SemanticSearchHit(
                component=_row_to_component(rows[int(i)]),
                score=float(scores[int(i)]),
            )
            for i in top_idx
        ]

    # ------------------------------------------------------------------
    # Run tracking
    # ------------------------------------------------------------------

    def start_run(self, org_alias: str, component_types: list[str]) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO index_runs (org_alias, started_at, component_types)
            VALUES (?, ?, ?)
            """,
            (org_alias, _now_iso(), json.dumps(component_types)),
        )
        self._conn.commit()
        return cur.lastrowid or 0

    def finish_run(
        self, run_id: int, components_count: int, error: str | None = None
    ) -> None:
        self._conn.execute(
            """
            UPDATE index_runs
            SET completed_at = ?, components_count = ?, error = ?
            WHERE id = ?
            """,
            (_now_iso(), components_count, error, run_id),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _exists(self, component_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM components WHERE id = ?", (component_id,)
        ).fetchone()
        return row is not None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_component(row: sqlite3.Row) -> ComponentRow:
    return ComponentRow(
        id=row["id"],
        component_type=row["component_type"],
        api_name=row["api_name"],
        parent_id=row["parent_id"],
        file_path=row["file_path"],
        source=row["source"],
        metadata=json.loads(row["metadata_json"] or "{}"),
        last_indexed_at=row["last_indexed_at"],
    )
