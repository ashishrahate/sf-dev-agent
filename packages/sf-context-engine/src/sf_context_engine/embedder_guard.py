"""Guard against mixing embedding models inside one database.

Vectors are stored as bare float32 BLOBs, so switching embedder (different
model or dimension) silently corrupts similarity search. This module records
which embedder produced the vectors in a single-row `embedder_meta` table and
refuses to embed or search with a different one until the vectors are reset.

States returned by `check()`:
    unset      no vectors yet and nothing recorded (first use)
    ok         recorded embedder matches the active one
    legacy_ok  DB predates the guard but its vectors match the active dimension
    mismatch   recorded name/dimension (or legacy vector dimension) differs
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sf_context_engine.embedders.base import Embedder
from sf_context_engine.paths import SCHEMA_PATH

# (table, hash column) pairs that hold embeddings.
_VECTOR_TABLES: tuple[tuple[str, str], ...] = (
    ("components", "embedded_source_hash"),
    ("knowledge_entries", "embedded_text_hash"),
    ("memories", "embedded_text_hash"),
)


class EmbedderMismatch(Exception):
    """Raised when the active embedder differs from the one that built the DB."""


@dataclass
class GuardStatus:
    state: str
    active_name: str
    active_dim: int
    recorded_name: str | None = None
    recorded_dim: int | None = None
    reason: str = ""

    @property
    def mismatch(self) -> bool:
        return self.state == "mismatch"

    def to_error(self) -> dict[str, object]:
        return {
            "error": "embedder_mismatch",
            "detail": self.reason,
            "recorded_embedder": self.recorded_name,
            "active_embedder": self.active_name,
            "hint": (
                "Switch back to the embedder that built this index (SF_CONTEXT_EMBEDDER), "
                "or discard the stored vectors and recompute them with the active one "
                "by calling an embed tool with reset_embeddings=true."
            ),
        }


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def _stored_dims(conn: sqlite3.Connection) -> set[int]:
    dims: set[int] = set()
    for table, _ in _VECTOR_TABLES:
        for row in conn.execute(
            f"SELECT DISTINCT length(embedding) AS n FROM {table} WHERE embedding IS NOT NULL"
        ):
            dims.add(int(row["n"]) // 4)
    return dims


def check(db_path: Path, embedder: Embedder) -> GuardStatus:
    """Compare the active embedder with what the DB recorded (read-only for new DBs)."""
    base = {"active_name": embedder.name, "active_dim": embedder.dim}
    if not Path(db_path).exists():
        return GuardStatus(state="unset", **base)
    conn = _connect(Path(db_path))
    try:
        row = conn.execute("SELECT name, dim FROM embedder_meta WHERE id = 1").fetchone()
        if row is not None:
            if row["name"] != embedder.name or int(row["dim"]) != embedder.dim:
                return GuardStatus(
                    state="mismatch", recorded_name=row["name"], recorded_dim=int(row["dim"]),
                    reason=(
                        f"index was built with {row['name']} ({row['dim']}-d) but the active "
                        f"embedder is {embedder.name} ({embedder.dim}-d)"
                    ),
                    **base,
                )
            return GuardStatus(
                state="ok", recorded_name=row["name"], recorded_dim=int(row["dim"]), **base,
            )
        dims = _stored_dims(conn)
        if not dims:
            return GuardStatus(state="unset", **base)
        if dims == {embedder.dim}:
            return GuardStatus(state="legacy_ok", recorded_dim=embedder.dim, **base)
        return GuardStatus(
            state="mismatch", recorded_dim=min(dims),
            reason=(
                f"existing vectors are {sorted(dims)}-d (embedder not recorded) but the "
                f"active embedder {embedder.name} produces {embedder.dim}-d"
            ),
            **base,
        )
    finally:
        conn.close()


def _record(conn: sqlite3.Connection, embedder: Embedder) -> None:
    conn.execute(
        "INSERT INTO embedder_meta (id, name, dim, set_at) VALUES (1, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET name = excluded.name, dim = excluded.dim, "
        "set_at = excluded.set_at",
        (embedder.name, embedder.dim, datetime.now(UTC).isoformat()),
    )
    conn.commit()


def claim(db_path: Path, embedder: Embedder, *, reset: bool = False) -> GuardStatus:
    """Record `embedder` as the DB's embedder, or raise EmbedderMismatch.

    With reset=True, every stored vector is discarded first so the active
    embedder can rebuild them (the source rows are untouched).
    """
    status = check(db_path, embedder)
    if status.mismatch and not reset:
        raise EmbedderMismatch(status.reason)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(Path(db_path))
    try:
        if status.mismatch and reset:
            for table, hash_col in _VECTOR_TABLES:
                conn.execute(f"UPDATE {table} SET embedding = NULL, {hash_col} = NULL")
        _record(conn, embedder)
    finally:
        conn.close()
    return check(db_path, embedder)
