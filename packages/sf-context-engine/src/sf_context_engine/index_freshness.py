"""Context-engine freshness detection — for auto-warm + agent self-check.

Phase B.2 of the post-Wave-8 roadmap. Two layers:

    Layer A — first-run prompt. On agent startup, if the org has no
              recorded index build, soft-prompt the user to warm the
              engine (build index + embed components + embed knowledge).
              Strict opt-in; never auto-runs without consent.

    Layer B — staleness check on every task. Inject a one-line freshness
              note into the agent's system prompt at runtime so the LLM
              knows when the index is stale and can call
              `build_metadata_index --delta` mid-task.

This module is purely the data layer: it queries `index_runs` +
`components.embedding` to compute an `IndexFreshness` snapshot. The
actual warmup runner and prompt UX live in `__main__.py` (Layer A) and
the agent's system prompt template (Layer B).

Public API:
    IndexFreshness(last_built_at, age_seconds, is_stale,
                   embedding_coverage_pct, components_count,
                   embedded_count, last_run_error)
    check_freshness(db_path, org_alias) -> IndexFreshness
    format_age_human(seconds) -> str           # "31 hours ago"
    format_freshness_line(freshness) -> str    # one-liner for the prompt
    stale_after_hours() -> float                # env-tunable threshold
    warmup_skip_path(db_path, org_alias) -> Path
    is_warmup_skipped(db_path, org_alias) -> bool
    mark_warmup_skipped(db_path, org_alias) -> None

The threshold for "stale" is read from `INDEX_STALE_AFTER_HOURS` env
(default 24h). A non-existent or empty index is reported as
`last_built_at is None` — separate from "stale" so the UX can decide
what to do (warmup prompt vs. delta refresh nudge).
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


_DEFAULT_STALE_HOURS: float = 24.0


def stale_after_hours() -> float:
    """Read the staleness threshold from env (`INDEX_STALE_AFTER_HOURS`)."""
    raw = os.environ.get("INDEX_STALE_AFTER_HOURS", "").strip()
    if not raw:
        return _DEFAULT_STALE_HOURS
    try:
        val = float(raw)
        if val <= 0:
            return _DEFAULT_STALE_HOURS
        return val
    except ValueError:
        return _DEFAULT_STALE_HOURS


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------

@dataclass
class IndexFreshness:
    """Snapshot of when the org's index was last built + how covered it is."""
    org_alias: str
    last_built_at: str | None         # ISO-8601 UTC; None means "never built"
    age_seconds: float | None         # None when never built
    is_stale: bool                    # last_built_at older than staleness threshold
    embedding_coverage_pct: float     # 0.0–100.0 (0 if no components)
    components_count: int
    embedded_count: int
    last_run_error: str | None        # last attempt's error, if any


# ---------------------------------------------------------------------------
# Core query
# ---------------------------------------------------------------------------

def check_freshness(db_path: Path | str, org_alias: str) -> IndexFreshness:
    """Compute an IndexFreshness for `org_alias` from the SQLite store.

    Returns a `last_built_at=None` snapshot if the DB doesn't exist or
    has no completed runs for the org. Always safe to call — never raises
    for missing files.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return IndexFreshness(
            org_alias=org_alias,
            last_built_at=None,
            age_seconds=None,
            is_stale=False,
            embedding_coverage_pct=0.0,
            components_count=0,
            embedded_count=0,
            last_run_error=None,
        )

    threshold_hours = stale_after_hours()
    threshold_seconds = threshold_hours * 3600.0

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        logger.warning("Could not open %s for freshness check: %s", db_path, exc)
        return IndexFreshness(
            org_alias=org_alias,
            last_built_at=None,
            age_seconds=None,
            is_stale=False,
            embedding_coverage_pct=0.0,
            components_count=0,
            embedded_count=0,
            last_run_error=str(exc),
        )

    try:
        # Latest successful (no-error, completed_at set) run for this org.
        run_row = conn.execute(
            """
            SELECT completed_at, error
            FROM index_runs
            WHERE org_alias = ? AND completed_at IS NOT NULL
            ORDER BY completed_at DESC
            LIMIT 1
            """,
            (org_alias,),
        ).fetchone()

        # Latest error (regardless of success) — surfaced for the UX.
        err_row = conn.execute(
            """
            SELECT error FROM index_runs
            WHERE org_alias = ? AND error IS NOT NULL
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (org_alias,),
        ).fetchone()
        last_run_error = err_row["error"] if err_row else None

        # Coverage stats — across all components, not org-scoped.
        # Org-scoping isn't on the components table today, so coverage is a
        # global signal. Good enough for the freshness UX.
        cov_row = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN embedding IS NOT NULL THEN 1 ELSE 0 END) AS embedded
            FROM components
            """
        ).fetchone()
        total = int(cov_row["total"] or 0)
        embedded = int(cov_row["embedded"] or 0)
        coverage_pct = (embedded / total * 100.0) if total > 0 else 0.0
    except sqlite3.Error as exc:
        # Table missing (DB pre-dates Wave 1?), schema mismatch, etc.
        # Treat as "never built" — strictly safer than crashing.
        logger.info("Freshness query failed: %s", exc)
        conn.close()
        return IndexFreshness(
            org_alias=org_alias,
            last_built_at=None,
            age_seconds=None,
            is_stale=False,
            embedding_coverage_pct=0.0,
            components_count=0,
            embedded_count=0,
            last_run_error=str(exc),
        )
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass

    if run_row is None:
        return IndexFreshness(
            org_alias=org_alias,
            last_built_at=None,
            age_seconds=None,
            is_stale=False,
            embedding_coverage_pct=coverage_pct,
            components_count=total,
            embedded_count=embedded,
            last_run_error=last_run_error,
        )

    last_built_at = run_row["completed_at"]
    age_seconds = _age_seconds(last_built_at)
    is_stale = age_seconds is not None and age_seconds > threshold_seconds

    return IndexFreshness(
        org_alias=org_alias,
        last_built_at=last_built_at,
        age_seconds=age_seconds,
        is_stale=is_stale,
        embedding_coverage_pct=coverage_pct,
        components_count=total,
        embedded_count=embedded,
        last_run_error=last_run_error,
    )


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

def format_age_human(seconds: float | None) -> str:
    """Round age into a single-unit human phrase: '31 hours ago', '4 days ago'."""
    if seconds is None or seconds < 0:
        return "unknown"
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        m = int(seconds // 60)
        return f"{m} minute{'s' if m != 1 else ''} ago"
    if seconds < 86400:
        h = int(seconds // 3600)
        return f"{h} hour{'s' if h != 1 else ''} ago"
    d = int(seconds // 86400)
    return f"{d} day{'s' if d != 1 else ''} ago"


def format_freshness_line(freshness: IndexFreshness) -> str:
    """One-line summary suitable for the agent's system prompt + the CLI.

    Covers four cases:
      - never built       → "NOT BUILT for this org"
      - built recently    → "last built X ago — Y% embedded"
      - built but stale   → "last built X ago, STALE — call build_metadata_index --delta"
      - empty coverage    → notes the embedding gap
    """
    if freshness.last_built_at is None:
        if freshness.components_count == 0:
            return (
                f"index NOT BUILT for {freshness.org_alias} — "
                "call build_metadata_index to populate the metadata index"
            )
        # Components exist but no run record (edge case — manual SQL?).
        return (
            f"index has {freshness.components_count} components but no "
            f"recorded build for {freshness.org_alias} — "
            "run build_metadata_index to refresh provenance"
        )

    age_phrase = format_age_human(freshness.age_seconds)
    coverage_phrase = f"{freshness.embedding_coverage_pct:.0f}% embedded"

    if freshness.is_stale:
        return (
            f"index last built {age_phrase}, STALE — "
            f"call build_metadata_index --delta if you suspect outdated data; "
            f"{coverage_phrase}"
        )

    return f"index last built {age_phrase}; {coverage_phrase}"


# ---------------------------------------------------------------------------
# Per-org skip flag for the warmup prompt
# ---------------------------------------------------------------------------

def warmup_skip_path(db_path: Path | str, org_alias: str) -> Path:
    """Path to the per-org sentinel file that suppresses the warmup prompt."""
    db_path = Path(db_path)
    cache_dir = db_path.parent
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in org_alias)
    return cache_dir / f".skip_warmup_{safe}"


def is_warmup_skipped(db_path: Path | str, org_alias: str) -> bool:
    return warmup_skip_path(db_path, org_alias).exists()


def mark_warmup_skipped(db_path: Path | str, org_alias: str) -> None:
    """Persist the user's "no-and-stop-asking" choice for this org."""
    path = warmup_skip_path(db_path, org_alias)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"warmup-prompt suppressed for {org_alias}\n"
        f"created at {datetime.now(UTC).isoformat(timespec='seconds')}\n"
        "delete this file to re-enable the prompt\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _age_seconds(iso_timestamp: str | None) -> float | None:
    if not iso_timestamp:
        return None
    try:
        ts = datetime.fromisoformat(iso_timestamp)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
    except ValueError:
        return None
    delta = datetime.now(UTC) - ts
    return max(0.0, delta.total_seconds())
