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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

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


class MetadataIndex:
    """Read/write interface to the SQLite metadata index."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

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
        """Return every ApexTrigger registered as TRIGGERS_ON the given object."""
        target_id = f"CustomObject:{object_api_name}"
        rows = self._conn.execute(
            """
            SELECT c.* FROM components c
            JOIN relationships r ON r.source_id = c.id
            WHERE r.target_id = ? AND r.relationship_type = 'TRIGGERS_ON'
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
