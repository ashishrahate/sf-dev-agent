"""SQLite-backed knowledge base.

Mirrors the patterns in `MetadataIndex` (BLOB embeddings, hash-gated refresh,
cosine-sim search via numpy) but on its own table so org-component queries
never get tangled with platform-knowledge queries.

Markdown ingestion uses a small in-house frontmatter parser — we don't pull
in PyYAML for what amounts to "split on `---` and parse a handful of keys."
The frontmatter format we accept:

    ---
    id: <kebab-case-stable-id>
    title: <human title>
    category: governor_limit | anti_pattern | best_practice | pattern
    severity: critical | high | medium | low | info
    tags: [tag1, tag2, ...]
    references:
      - https://...
      - https://...
    ---

    Markdown body...
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from sf_dev_agent.context.embedders.base import Embedder, hash_text

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------

@dataclass
class KnowledgeEntry:
    """A row from `knowledge_entries`, hydrated for callers."""
    id: str
    title: str
    category: str
    severity: str | None
    tags: list[str]
    references: list[str]
    body: str
    file_path: str | None
    last_loaded_at: str


@dataclass
class KnowledgeSearchHit:
    entry: KnowledgeEntry
    score: float


@dataclass
class KnowledgeIngestResult:
    loaded: int = 0
    updated: int = 0
    skipped_unchanged: int = 0
    parse_errors: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class KnowledgeEmbedResult:
    embedded: int = 0
    skipped_unchanged: int = 0
    errors: list[str] = field(default_factory=list)
    embedder_name: str = ""


# ---------------------------------------------------------------------------
# Bundled entries dir
# ---------------------------------------------------------------------------

def bundled_entries_dir() -> Path:
    """Path to the Markdown entries shipped with the package."""
    return Path(__file__).parent / "entries"


# ---------------------------------------------------------------------------
# Frontmatter parser (no external deps)
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.DOTALL)


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a Markdown file into (frontmatter dict, body string).

    Supports flat `key: value`, inline lists `[a, b, c]`, and block lists:
        references:
          - https://...
          - https://...
    Anything fancier (nested mappings, multi-line strings) is intentionally
    out of scope — knowledge entries should keep their frontmatter simple.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    fm_text, body = match.group(1), match.group(2)
    return _parse_frontmatter_lines(fm_text), body.strip()


def _parse_frontmatter_lines(fm_text: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    current_list_key: str | None = None
    for raw in fm_text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            current_list_key = None
            continue
        # block-list continuation
        if line.startswith(("  -", "\t-")) and current_list_key:
            value = line.lstrip(" \t-").strip()
            result.setdefault(current_list_key, []).append(_unquote_list_item(value))
            continue
        # key: value
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            if value == "":
                # block-list header
                current_list_key = key
                result[key] = []
                continue
            current_list_key = None
            result[key] = _parse_inline_value(value)
    return result


def _parse_inline_value(value: str) -> Any:
    # JSON-style quoted scalar — used by the memory exporter for any value
    # that contains YAML-significant chars (colons, brackets, etc.). Lets
    # us round-trip rich content without a full YAML library.
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            pass
    if value == "null":
        return None
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_unquote_list_item(item.strip()) for item in inner.split(",")]
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    return value


def _unquote_list_item(item: str) -> str:
    """Strip JSON-style quoting from a single inline-list element."""
    if len(item) >= 2 and item.startswith('"') and item.endswith('"'):
        try:
            return json.loads(item)
        except json.JSONDecodeError:
            return item
    return item


# ---------------------------------------------------------------------------
# KnowledgeBase
# ---------------------------------------------------------------------------

class KnowledgeBase:
    """Read/write interface to the SQLite knowledge table."""

    def __init__(
        self,
        db_path: Path | str,
        entries_dir: Path | str | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        # Ensures the knowledge_entries table exists for existing DBs that
        # were created before slice 3.
        schema_path = Path(__file__).resolve().parent.parent / "schema.sql"
        self._conn.executescript(schema_path.read_text(encoding="utf-8"))

        self.entries_dir = Path(entries_dir) if entries_dir else bundled_entries_dir()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "KnowledgeBase":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Auto-load + ingestion
    # ------------------------------------------------------------------

    def auto_load_if_empty(self) -> KnowledgeIngestResult:
        """Ingest bundled entries if the table is empty. Idempotent."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM knowledge_entries"
        ).fetchone()
        if (row["n"] or 0) > 0:
            return KnowledgeIngestResult()
        return self.load_entries(self.entries_dir)

    def load_entries(self, entries_dir: Path | str | None = None) -> KnowledgeIngestResult:
        """Walk `entries_dir/**/*.md`, parse, upsert into the index.

        Idempotent — running twice with no changes leaves the DB unchanged
        (same id + same body content => no row writes).
        """
        directory = Path(entries_dir) if entries_dir else self.entries_dir
        if not directory.exists():
            return KnowledgeIngestResult(parse_errors=[
                (str(directory), "entries directory does not exist"),
            ])

        result = KnowledgeIngestResult()
        for md_path in sorted(directory.rglob("*.md")):
            try:
                self._upsert_from_file(md_path, result)
            except Exception as exc:
                result.parse_errors.append(
                    (str(md_path), f"{type(exc).__name__}: {exc}")
                )
                logger.warning("Knowledge entry failed: %s -> %s", md_path, exc)

        self._conn.commit()
        return result

    def _upsert_from_file(self, path: Path, result: KnowledgeIngestResult) -> None:
        text = path.read_text(encoding="utf-8")
        fm, body = _parse_frontmatter(text)

        entry_id = fm.get("id")
        if not entry_id:
            raise ValueError("frontmatter missing required 'id'")
        title = fm.get("title") or entry_id
        category = fm.get("category") or "uncategorized"
        severity = fm.get("severity")
        tags = fm.get("tags") or []
        references = fm.get("references") or []
        if not isinstance(tags, list):
            tags = [tags]
        if not isinstance(references, list):
            references = [references]

        # Detect changes: compare body+title hash against what's stored. This is
        # not the same hash as `embedded_text_hash` (which gates re-embedding);
        # it's a simple "did the source file actually change?" check.
        existing = self._conn.execute(
            "SELECT body, title, category, severity, tags_json, references_json "
            "FROM knowledge_entries WHERE id = ?",
            (entry_id,),
        ).fetchone()

        tags_json = json.dumps(tags)
        refs_json = json.dumps(references)

        if existing is not None:
            unchanged = (
                existing["body"] == body
                and existing["title"] == title
                and existing["category"] == category
                and (existing["severity"] or "") == (severity or "")
                and existing["tags_json"] == tags_json
                and existing["references_json"] == refs_json
            )
            if unchanged:
                result.skipped_unchanged += 1
                return

        self._conn.execute(
            """
            INSERT INTO knowledge_entries (
                id, title, category, severity, tags_json,
                references_json, body, file_path, last_loaded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title,
                category = excluded.category,
                severity = excluded.severity,
                tags_json = excluded.tags_json,
                references_json = excluded.references_json,
                body = excluded.body,
                file_path = excluded.file_path,
                last_loaded_at = excluded.last_loaded_at
            """,
            (
                entry_id, title, category, severity or None,
                tags_json, refs_json, body,
                str(path), _now_iso(),
            ),
        )
        if existing is None:
            result.loaded += 1
        else:
            result.updated += 1

    # ------------------------------------------------------------------
    # Embeddings
    # ------------------------------------------------------------------

    @staticmethod
    def _embedding_text(row: sqlite3.Row) -> str:
        """Compose the canonical text we embed for a knowledge entry.

        Title + category + tags up front gives strong signal for queries
        like "governor limit on heap" or "trigger framework"; the body
        provides the fine-grained semantic content.
        """
        tags = " ".join(json.loads(row["tags_json"] or "[]"))
        return (
            f"{row['category']} {row['severity'] or ''} {tags}\n"
            f"{row['title']}\n\n"
            f"{row['body']}"
        )

    def embed_entries(
        self,
        embedder: Embedder,
        category: str | None = None,
        force: bool = False,
        batch_size: int = 32,
    ) -> KnowledgeEmbedResult:
        """Hash-gated embed/refresh — same pattern as MetadataIndex."""
        result = KnowledgeEmbedResult(embedder_name=embedder.name)

        if category:
            rows = self._conn.execute(
                "SELECT * FROM knowledge_entries WHERE category = ?",
                (category,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM knowledge_entries"
            ).fetchall()

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
                logger.error("Knowledge embedding batch failed: %s", exc)
                continue

            for (entry_id, _, content_hash), vec in zip(chunk, vectors):
                blob = np.asarray(vec, dtype=np.float32).tobytes()
                self._conn.execute(
                    """
                    UPDATE knowledge_entries
                    SET embedding = ?, embedded_text_hash = ?
                    WHERE id = ?
                    """,
                    (blob, content_hash, entry_id),
                )
                result.embedded += 1

        self._conn.commit()
        return result

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def find_by_id(self, entry_id: str) -> KnowledgeEntry | None:
        row = self._conn.execute(
            "SELECT * FROM knowledge_entries WHERE id = ?",
            (entry_id,),
        ).fetchone()
        return _row_to_entry(row) if row else None

    def find_by_category(self, category: str) -> list[KnowledgeEntry]:
        rows = self._conn.execute(
            "SELECT * FROM knowledge_entries WHERE category = ? ORDER BY id",
            (category,),
        ).fetchall()
        return [_row_to_entry(r) for r in rows]

    def stats(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT category, COUNT(*) AS n FROM knowledge_entries GROUP BY category"
        ).fetchall()
        return {r["category"]: r["n"] for r in rows}

    def embedding_stats(self) -> dict[str, dict[str, int]]:
        rows = self._conn.execute(
            """
            SELECT
                category,
                COUNT(*) AS total,
                SUM(CASE WHEN embedding IS NOT NULL THEN 1 ELSE 0 END) AS embedded
            FROM knowledge_entries
            GROUP BY category
            """
        ).fetchall()
        return {
            r["category"]: {"total": r["total"], "embedded": r["embedded"]}
            for r in rows
        }

    def search(
        self,
        query_embedding: np.ndarray,
        category: str | None = None,
        limit: int = 10,
    ) -> list[KnowledgeSearchHit]:
        """Cosine-sim ranking over knowledge entries with an embedding."""
        query = np.asarray(query_embedding, dtype=np.float32)
        norm = np.linalg.norm(query)
        if norm > 0:
            query = query / norm

        if category:
            rows = self._conn.execute(
                """
                SELECT * FROM knowledge_entries
                WHERE embedding IS NOT NULL AND category = ?
                """,
                (category,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM knowledge_entries WHERE embedding IS NOT NULL"
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

        return [
            KnowledgeSearchHit(
                entry=_row_to_entry(rows[int(i)]),
                score=float(scores[int(i)]),
            )
            for i in top_idx
        ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_entry(row: sqlite3.Row) -> KnowledgeEntry:
    return KnowledgeEntry(
        id=row["id"],
        title=row["title"],
        category=row["category"],
        severity=row["severity"],
        tags=json.loads(row["tags_json"] or "[]"),
        references=json.loads(row["references_json"] or "[]"),
        body=row["body"],
        file_path=row["file_path"],
        last_loaded_at=row["last_loaded_at"],
    )
