"""Unit tests for index freshness detection (Phase B.2 Layer A + B).

Covers the data layer (`check_freshness`, formatters, skip-flag), the
system-prompt injection (verifying agent.py renders {{INDEX_FRESHNESS}}
with a real freshness line), and the `check_index_freshness` tool wired
into the registry.

Warmup runner is exercised via mocks — no real `sf` CLI calls or Gemini
embeddings.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sf_dev_agent.context import MetadataIndex
from sf_dev_agent.index_freshness import (
    check_freshness,
    format_age_human,
    format_freshness_line,
    is_warmup_skipped,
    mark_warmup_skipped,
    stale_after_hours,
    warmup_skip_path,
)
from sf_dev_agent.models.schemas import OrgConnection
from sf_dev_agent.tools.registry import ToolRegistry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def org() -> OrgConnection:
    return OrgConnection(
        tenant_id="t1",
        org_alias="OrgA",
        org_type="developer",
        instance_url="https://example.salesforce.com",
    )


def _seed_run(db: Path, org_alias: str, completed_at: str | None,
              error: str | None = None) -> None:
    """Insert a row into index_runs directly."""
    # Open via MetadataIndex so the schema is created if needed.
    MetadataIndex(db).close()
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        INSERT INTO index_runs (org_alias, started_at, completed_at,
                                component_types, components_count, error)
        VALUES (?, ?, ?, '[]', 0, ?)
        """,
        (org_alias, completed_at or "2026-04-27T00:00:00+00:00",
         completed_at, error),
    )
    conn.commit()
    conn.close()


def _isoformat_minus(seconds: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat(
        timespec="seconds"
    )


# ---------------------------------------------------------------------------
# stale_after_hours env reading
# ---------------------------------------------------------------------------

def test_stale_after_hours_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INDEX_STALE_AFTER_HOURS", raising=False)
    assert stale_after_hours() == 24.0


def test_stale_after_hours_custom(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INDEX_STALE_AFTER_HOURS", "2")
    assert stale_after_hours() == 2.0


def test_stale_after_hours_invalid_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INDEX_STALE_AFTER_HOURS", "not-a-number")
    assert stale_after_hours() == 24.0


def test_stale_after_hours_negative_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INDEX_STALE_AFTER_HOURS", "-5")
    assert stale_after_hours() == 24.0


# ---------------------------------------------------------------------------
# check_freshness — empty / missing / coverage / staleness
# ---------------------------------------------------------------------------

def test_freshness_when_db_missing(tmp_path: Path) -> None:
    f = check_freshness(tmp_path / "nope.db", org_alias="OrgA")
    assert f.last_built_at is None
    assert f.age_seconds is None
    assert f.is_stale is False
    assert f.components_count == 0
    assert f.embedding_coverage_pct == 0.0


def test_freshness_when_index_empty(tmp_path: Path) -> None:
    db = tmp_path / "empty.db"
    MetadataIndex(db).close()
    f = check_freshness(db, org_alias="OrgA")
    assert f.last_built_at is None
    assert f.components_count == 0


def test_freshness_recent_run_not_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INDEX_STALE_AFTER_HOURS", "24")
    db = tmp_path / "recent.db"
    _seed_run(db, "OrgA", completed_at=_isoformat_minus(seconds=3600))
    f = check_freshness(db, org_alias="OrgA")
    assert f.last_built_at is not None
    assert f.is_stale is False
    assert f.age_seconds is not None and 3500 <= f.age_seconds <= 3700


def test_freshness_old_run_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INDEX_STALE_AFTER_HOURS", "24")
    db = tmp_path / "old.db"
    _seed_run(db, "OrgA", completed_at=_isoformat_minus(seconds=48 * 3600))
    f = check_freshness(db, org_alias="OrgA")
    assert f.is_stale is True


def test_freshness_scoped_per_org(tmp_path: Path) -> None:
    """A run for OrgA must not surface as the latest run for OrgB."""
    db = tmp_path / "scoped.db"
    _seed_run(db, "OrgA", completed_at=_isoformat_minus(seconds=60))
    f_b = check_freshness(db, org_alias="OrgB")
    assert f_b.last_built_at is None


def test_freshness_picks_latest_completed_run(tmp_path: Path) -> None:
    db = tmp_path / "many.db"
    _seed_run(db, "OrgA", completed_at="2026-01-01T00:00:00+00:00")
    _seed_run(db, "OrgA", completed_at="2026-04-01T00:00:00+00:00")
    _seed_run(db, "OrgA", completed_at="2026-02-01T00:00:00+00:00")
    f = check_freshness(db, org_alias="OrgA")
    assert f.last_built_at == "2026-04-01T00:00:00+00:00"


def test_freshness_skips_incomplete_runs(tmp_path: Path) -> None:
    """A run with completed_at NULL is mid-flight and shouldn't count."""
    db = tmp_path / "midflight.db"
    _seed_run(db, "OrgA", completed_at=None)  # mid-flight
    f = check_freshness(db, org_alias="OrgA")
    assert f.last_built_at is None


def test_freshness_surfaces_last_error(tmp_path: Path) -> None:
    db = tmp_path / "errored.db"
    _seed_run(
        db, "OrgA", completed_at=None,
        error="DomainNotFoundError: org went away",
    )
    f = check_freshness(db, org_alias="OrgA")
    assert f.last_run_error is not None
    assert "DomainNotFoundError" in f.last_run_error


def test_freshness_embedding_coverage(tmp_path: Path) -> None:
    db = tmp_path / "cov.db"
    with MetadataIndex(db) as idx:
        # Insert two component rows directly: one with embedding, one without.
        idx._conn.execute(
            "INSERT INTO components (id, component_type, api_name, "
            "metadata_json, last_indexed_at, embedding) "
            "VALUES ('a', 'ApexClass', 'A', '{}', '2026-04-27', X'00')"
        )
        idx._conn.execute(
            "INSERT INTO components (id, component_type, api_name, "
            "metadata_json, last_indexed_at) "
            "VALUES ('b', 'ApexClass', 'B', '{}', '2026-04-27')"
        )
        idx._conn.commit()
    f = check_freshness(db, org_alias="OrgA")
    assert f.components_count == 2
    assert f.embedded_count == 1
    assert f.embedding_coverage_pct == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("seconds,expected", [
    (None, "unknown"),
    (-5, "unknown"),
    (10, "just now"),
    (60, "1 minute ago"),
    (180, "3 minutes ago"),
    (3600, "1 hour ago"),
    (3 * 3600, "3 hours ago"),
    (86400, "1 day ago"),
    (3 * 86400, "3 days ago"),
])
def test_format_age_human(seconds: float | None, expected: str) -> None:
    assert format_age_human(seconds) == expected


def test_freshness_line_never_built() -> None:
    from sf_dev_agent.index_freshness import IndexFreshness
    f = IndexFreshness(
        org_alias="OrgA", last_built_at=None, age_seconds=None,
        is_stale=False, embedding_coverage_pct=0.0,
        components_count=0, embedded_count=0, last_run_error=None,
    )
    line = format_freshness_line(f)
    assert "NOT BUILT" in line
    assert "OrgA" in line


def test_freshness_line_recent_built() -> None:
    from sf_dev_agent.index_freshness import IndexFreshness
    f = IndexFreshness(
        org_alias="OrgA", last_built_at="2026-04-27T00:00:00+00:00",
        age_seconds=3600, is_stale=False, embedding_coverage_pct=87.5,
        components_count=8, embedded_count=7, last_run_error=None,
    )
    line = format_freshness_line(f)
    assert "1 hour ago" in line
    assert "88% embedded" in line  # rounded to nearest int by format spec
    assert "STALE" not in line


def test_freshness_line_stale() -> None:
    from sf_dev_agent.index_freshness import IndexFreshness
    f = IndexFreshness(
        org_alias="OrgA", last_built_at="2026-04-25T00:00:00+00:00",
        age_seconds=48 * 3600, is_stale=True, embedding_coverage_pct=100.0,
        components_count=5, embedded_count=5, last_run_error=None,
    )
    line = format_freshness_line(f)
    assert "STALE" in line
    assert "build_metadata_index" in line


# ---------------------------------------------------------------------------
# Skip-flag persistence
# ---------------------------------------------------------------------------

def test_skip_flag_round_trip(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"")
    assert is_warmup_skipped(db, "OrgA") is False
    mark_warmup_skipped(db, "OrgA")
    assert is_warmup_skipped(db, "OrgA") is True
    assert warmup_skip_path(db, "OrgA").exists()


def test_skip_flag_per_org(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"")
    mark_warmup_skipped(db, "OrgA")
    assert is_warmup_skipped(db, "OrgA") is True
    assert is_warmup_skipped(db, "OrgB") is False


def test_skip_flag_handles_unsafe_alias_chars(tmp_path: Path) -> None:
    """Aliases with slashes or special chars get sanitized in the filename."""
    db = tmp_path / "memory.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"")
    weird = "my/org:with*chars"
    mark_warmup_skipped(db, weird)
    assert is_warmup_skipped(db, weird) is True
    # File exists on disk — no path traversal.
    path = warmup_skip_path(db, weird)
    assert path.exists()
    assert "/" not in path.name
    assert ":" not in path.name


# ---------------------------------------------------------------------------
# check_index_freshness tool — registry round-trip
# ---------------------------------------------------------------------------

def test_check_index_freshness_tool_registered(org: OrgConnection) -> None:
    registry = ToolRegistry(org=org, mock_org=True)
    names = {t["name"] for t in registry.get_tool_definitions()}
    assert "check_index_freshness" in names


def test_check_index_freshness_tool_returns_shape(
    tmp_path: Path, org: OrgConnection,
) -> None:
    db = tmp_path / "tool.db"
    _seed_run(db, "OrgA", completed_at=_isoformat_minus(60))
    registry = ToolRegistry(org=org, mock_org=False, index_db_path=db)
    response = registry.execute("check_index_freshness", {})

    assert response["org_alias"] == "OrgA"
    assert response["last_built_at"] is not None
    assert "freshness_line" in response
    assert response["is_stale"] in (True, False)
    assert "embedding_coverage_pct" in response
    assert isinstance(response["components_count"], int)


def test_check_index_freshness_tool_handles_missing_db(
    tmp_path: Path, org: OrgConnection,
) -> None:
    db = tmp_path / "never_existed.db"
    registry = ToolRegistry(org=org, mock_org=False, index_db_path=db)
    response = registry.execute("check_index_freshness", {})
    assert response["last_built_at"] is None
    assert "NOT BUILT" in response["freshness_line"]


# ---------------------------------------------------------------------------
# Agent system-prompt injection
# ---------------------------------------------------------------------------

def test_agent_injects_freshness_into_system_prompt(
    tmp_path: Path, org: OrgConnection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AgentLoop renders {{INDEX_FRESHNESS}} from a real check_freshness call."""
    from sf_dev_agent.agent import AgentLoop
    from sf_dev_agent.providers.base import LLMProvider, LLMResponse

    class _StubProvider(LLMProvider):
        @property
        def model_name(self) -> str:
            return "stub"

        def chat(self, **kwargs):
            return LLMResponse(text_blocks=["x"], stop_reason="end_turn")

    db = tmp_path / "agent_freshness.db"
    _seed_run(db, "OrgA", completed_at=_isoformat_minus(60))

    # Point the agent at this temporary DB.
    monkeypatch.setattr(
        "sf_dev_agent.context.default_db_path",
        lambda: db,
    )

    agent = AgentLoop(org=org, provider=_StubProvider(), mock_org=True)
    assert "{{INDEX_FRESHNESS}}" not in agent.system_prompt
    # The recent run produces "X minutes ago" or "1 hour ago" inline.
    assert "Index freshness:" in agent.system_prompt
    assert "ago" in agent.system_prompt
