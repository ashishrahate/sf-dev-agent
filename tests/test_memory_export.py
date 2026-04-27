"""Unit tests for MemoryExporter (Wave 8 slice 3b).

Validates the Markdown round-trip: export -> file -> re-parse with the
same frontmatter parser the knowledge-base uses, recover the fields.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sf_dev_agent.context.knowledge.store import _parse_frontmatter
from sf_dev_agent.memory import MemoryScope, MemoryStore
from sf_dev_agent.memory.export import MemoryExporter, default_export_dir

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    s = MemoryStore(tmp_path / "memory.db")
    yield s
    s.close()


@pytest.fixture
def scope() -> MemoryScope:
    return MemoryScope(tenant_id="t1", org_alias="OrgA")


# ---------------------------------------------------------------------------
# Default location
# ---------------------------------------------------------------------------

def test_default_export_dir_is_under_cache() -> None:
    """The default export dir lives under .cache so it's not git-tracked."""
    assert default_export_dir().parts[-3:] == (".cache", "memory", "exports")


# ---------------------------------------------------------------------------
# Empty / no-match
# ---------------------------------------------------------------------------

def test_export_empty_store_writes_nothing(
    tmp_path: Path, store: MemoryStore, scope: MemoryScope,
) -> None:
    exporter = MemoryExporter(store=store, out_dir=tmp_path / "out")
    result = exporter.export(scope=scope)
    assert result.files == []
    assert result.skipped == 0


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------

def test_export_round_trips_through_frontmatter_parser(
    tmp_path: Path, store: MemoryStore, scope: MemoryScope,
) -> None:
    record = store.save(
        scope=scope,
        type="feedback",
        name="dedup-pref",
        description="prefer email + phone match for account dedup",
        body=(
            "Rule: match Email__c + Phone for Account dedup.\n"
            "**Why:** prior incident with bare-name match.\n"
            "**How to apply:** any new Account dedup logic."
        ),
        tags=["dedup", "accounts"],
        source_session_id="task_session_001",
    )

    out_dir = tmp_path / "out"
    exporter = MemoryExporter(store=store, out_dir=out_dir)
    result = exporter.export(scope=scope)

    assert len(result.files) == 1
    path = result.files[0]
    assert path.parent == out_dir
    assert path.suffix == ".md"
    assert "dedup-pref" in path.name
    assert path.name.startswith("feedback__")

    text = path.read_text(encoding="utf-8")
    fm, body = _parse_frontmatter(text)

    # Every column round-trips via the frontmatter parser.
    assert fm["id"] == record.id
    assert fm["tenant_id"] == record.tenant_id
    assert fm["org_alias"] == record.org_alias
    assert fm["type"] == record.type
    assert fm["name"] == record.name
    assert fm["description"] == record.description
    assert fm["tags"] == ["dedup", "accounts"]
    assert fm["source_session_id"] == "task_session_001"
    assert fm["created_at"] == record.created_at
    assert fm["last_accessed_at"] == record.last_accessed_at
    assert int(fm["access_count"]) == record.access_count

    # Body preserved verbatim.
    assert "match Email__c + Phone" in body
    assert "**Why:**" in body
    assert "**How to apply:**" in body


def test_export_handles_null_optional_fields(
    tmp_path: Path, store: MemoryStore,
) -> None:
    """A cross-org memory has org_alias=None — must export as YAML 'null'."""
    cross = MemoryScope(tenant_id="t1", org_alias=None)
    store.save(
        scope=cross, type="user", name="cross-pref",
        description="user pref everywhere", body="staff engineer",
    )

    out_dir = tmp_path / "out"
    exporter = MemoryExporter(store=store, out_dir=out_dir)
    result = exporter.export(scope=cross)
    assert len(result.files) == 1
    text = result.files[0].read_text(encoding="utf-8")
    assert "org_alias: null" in text
    assert "source_session_id: null" in text
    assert "superseded_by: null" in text


def test_export_quotes_values_with_special_chars(
    tmp_path: Path, store: MemoryStore, scope: MemoryScope,
) -> None:
    """Descriptions with colons / brackets must quote so YAML round-trips."""
    store.save(
        scope=scope, type="reference", name="grafana",
        description="grafana.internal/d/api-latency: oncall dashboard",
        body="x", tags=["dashboard", "oncall"],
    )

    out_dir = tmp_path / "out"
    exporter = MemoryExporter(store=store, out_dir=out_dir)
    result = exporter.export(scope=scope)

    text = result.files[0].read_text(encoding="utf-8")
    fm, _ = _parse_frontmatter(text)
    assert fm["description"] == "grafana.internal/d/api-latency: oncall dashboard"


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

def test_export_type_filter(
    tmp_path: Path, store: MemoryStore, scope: MemoryScope,
) -> None:
    store.save(scope=scope, type="user", name="role",
               description="x", body="x")
    store.save(scope=scope, type="feedback", name="fb",
               description="x", body="x")

    out_dir = tmp_path / "filt"
    exporter = MemoryExporter(store=store, out_dir=out_dir)
    result = exporter.export(scope=scope, type="user")

    assert len(result.files) == 1
    assert result.files[0].name.startswith("user__")


def test_export_excludes_superseded_by_default(
    tmp_path: Path, store: MemoryStore, scope: MemoryScope,
) -> None:
    old = store.save(scope=scope, type="feedback", name="old",
                     description="x", body="x")
    new = store.save(scope=scope, type="feedback", name="new",
                     description="x", body="x")
    store.supersede(old.id, new.id)

    out_dir = tmp_path / "noseen"
    exporter = MemoryExporter(store=store, out_dir=out_dir)
    result = exporter.export(scope=scope)
    names = {p.name for p in result.files}
    assert any("new" in n for n in names)
    assert not any("old" in n for n in names)

    # Opt-in pulls them all.
    out_dir2 = tmp_path / "withsuper"
    exporter2 = MemoryExporter(store=store, out_dir=out_dir2)
    result2 = exporter2.export(scope=scope, include_superseded=True)
    names2 = {p.name for p in result2.files}
    assert any("old" in n for n in names2)
    assert any("new" in n for n in names2)


# ---------------------------------------------------------------------------
# Filename collisions
# ---------------------------------------------------------------------------

def test_export_filenames_disambiguated_by_id_short(
    tmp_path: Path, store: MemoryStore,
) -> None:
    """Two memories with the same name in different scopes: the short-id
    suffix prevents the export from clobbering one with the other."""
    a = store.save(
        scope=MemoryScope(tenant_id="t1", org_alias="OrgA"),
        type="user", name="role", description="x", body="org A role",
    )
    b = store.save(
        scope=MemoryScope(tenant_id="t1", org_alias="OrgB"),
        type="user", name="role", description="x", body="org B role",
    )

    # Export each scope independently — but for the test, simulate hand-
    # exporting both into the same directory by writing each row directly.
    out_dir = tmp_path / "shared"
    out_dir.mkdir()
    exporter_a = MemoryExporter(store=store, out_dir=out_dir)
    exporter_a._write_record(a)
    exporter_a._write_record(b)

    files = sorted(out_dir.glob("*.md"))
    assert len(files) == 2  # didn't overwrite each other
    assert files[0].name != files[1].name
