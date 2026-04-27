"""Unit tests for MemoryPromoter (Wave 8 slice 3c).

Validates: knowledge-entry rendering, the tenant-specific-content heuristic
gate, the --force override, error paths.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sf_dev_agent.context.knowledge.store import _parse_frontmatter
from sf_dev_agent.memory import MemoryScope, MemoryStore
from sf_dev_agent.memory.promote import (
    KNOWLEDGE_CATEGORIES,
    MemoryPromoter,
)

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
    return MemoryScope(tenant_id="t1", org_alias="GenericOrg")


@pytest.fixture
def entries_dir(tmp_path: Path) -> Path:
    """Promotion target dir — kept separate from the shipped knowledge base."""
    return tmp_path / "knowledge_entries"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_promote_writes_valid_knowledge_entry(
    store: MemoryStore, scope: MemoryScope, entries_dir: Path,
) -> None:
    """A clean memory promotes to a parseable knowledge entry."""
    record = store.save(
        scope=scope, type="project", name="bulkify-trigger-loops",
        description="Trigger loops over collections must be bulkified",
        body=(
            "Rule: never call DML or SOQL inside a for-loop in a trigger.\n"
            "**Why:** governor limits hit at 101 SOQL / 151 DML per transaction.\n"
            "**How to apply:** any trigger touching List<SObject>."
        ),
        tags=["trigger", "bulkification"],
    )

    promoter = MemoryPromoter(store=store, entries_dir=entries_dir)
    result = promoter.promote(
        memory_id=record.id,
        category="best_practice",
        severity="high",
        references=["https://developer.salesforce.com/docs/atlas.en-us.apexcode.meta/apexcode/apex_gov_limits.htm"],
    )

    assert not result.skipped
    assert result.warnings == []
    assert result.file.exists()
    assert result.file.parent.name == "best_practice"

    text = result.file.read_text(encoding="utf-8")
    fm, body = _parse_frontmatter(text)

    assert fm["id"].startswith("bp-")
    assert fm["category"] == "best_practice"
    assert fm["severity"] == "high"
    assert fm["tags"] == ["trigger", "bulkification"]
    assert fm["references"] == [
        "https://developer.salesforce.com/docs/atlas.en-us.apexcode.meta/apexcode/apex_gov_limits.htm"
    ]
    # Body retained verbatim.
    assert "**Why:**" in body
    assert "**How to apply:**" in body


def test_promote_uses_memory_description_as_default_title(
    store: MemoryStore, scope: MemoryScope, entries_dir: Path,
) -> None:
    record = store.save(
        scope=scope, type="project", name="auto-title",
        description="A meaningful title from description",
        body="x",
    )
    promoter = MemoryPromoter(store=store, entries_dir=entries_dir)
    result = promoter.promote(memory_id=record.id, category="pattern")
    text = result.file.read_text(encoding="utf-8")
    fm, _ = _parse_frontmatter(text)
    assert fm["title"] == "A meaningful title from description"


def test_promote_explicit_title_overrides_description(
    store: MemoryStore, scope: MemoryScope, entries_dir: Path,
) -> None:
    record = store.save(
        scope=scope, type="project", name="explicit",
        description="boring description", body="x",
    )
    promoter = MemoryPromoter(store=store, entries_dir=entries_dir)
    result = promoter.promote(
        memory_id=record.id, category="pattern", title="Reframed Title",
    )
    text = result.file.read_text(encoding="utf-8")
    fm, _ = _parse_frontmatter(text)
    assert fm["title"] == "Reframed Title"


# ---------------------------------------------------------------------------
# Tenant-specific-content heuristic
# ---------------------------------------------------------------------------

def test_promote_blocks_when_org_alias_appears_in_body(
    store: MemoryStore, entries_dir: Path,
) -> None:
    scope = MemoryScope(tenant_id="t1", org_alias="AcmeProd")
    record = store.save(
        scope=scope, type="project", name="acme-quirk",
        description="x",
        body="In AcmeProd, the validation rule fails for null Industry.",
    )
    promoter = MemoryPromoter(store=store, entries_dir=entries_dir)
    result = promoter.promote(memory_id=record.id, category="anti_pattern")

    assert result.skipped is True
    assert any("AcmeProd" in w for w in result.warnings)
    assert not result.file.exists()


def test_promote_blocks_when_instance_url_in_body(
    store: MemoryStore, scope: MemoryScope, entries_dir: Path,
) -> None:
    record = store.save(
        scope=scope, type="project", name="url-leak",
        description="x",
        body="Hit https://acme.my.salesforce.com/services/data/v62.0/sobjects/Account",
    )
    promoter = MemoryPromoter(store=store, entries_dir=entries_dir)
    result = promoter.promote(memory_id=record.id, category="anti_pattern")
    assert result.skipped is True
    assert any("instance URL" in w for w in result.warnings)


def test_promote_blocks_on_likely_id_dump(
    store: MemoryStore, scope: MemoryScope, entries_dir: Path,
) -> None:
    record = store.save(
        scope=scope, type="project", name="ids",
        description="x",
        # 15-char Salesforce-shaped IDs.
        body="Failed records: 001A0000005ABCD and 001A0000006EFGH missing Industry.",
    )
    promoter = MemoryPromoter(store=store, entries_dir=entries_dir)
    result = promoter.promote(memory_id=record.id, category="anti_pattern")
    assert result.skipped is True
    assert any("Salesforce-shaped IDs" in w for w in result.warnings)


def test_promote_force_writes_with_warnings_recorded_in_draft(
    store: MemoryStore, entries_dir: Path,
) -> None:
    scope = MemoryScope(tenant_id="t1", org_alias="AcmeProd")
    record = store.save(
        scope=scope, type="project", name="forced",
        description="x",
        body="In AcmeProd, the validation rule fails — but this lesson generalizes.",
    )
    promoter = MemoryPromoter(store=store, entries_dir=entries_dir)
    result = promoter.promote(
        memory_id=record.id, category="anti_pattern", force=True,
    )
    assert result.skipped is False
    assert result.warnings  # warnings still recorded
    text = result.file.read_text(encoding="utf-8")
    # Heuristic warnings get inlined in the draft so the reviewer can't miss them.
    assert "PROMOTION REVIEW" in text
    assert "AcmeProd" in text


def test_promote_clean_content_has_no_warnings(
    store: MemoryStore, scope: MemoryScope, entries_dir: Path,
) -> None:
    record = store.save(
        scope=scope, type="project", name="clean",
        description="Salesforce best practice on null-safe SOQL",
        body="Always null-check optional lookup fields before chaining `.`",
        tags=["soql", "null-safety"],
    )
    promoter = MemoryPromoter(store=store, entries_dir=entries_dir)
    result = promoter.promote(memory_id=record.id, category="best_practice")
    assert result.skipped is False
    assert result.warnings == []


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------

def test_promote_unknown_memory_raises(
    store: MemoryStore, entries_dir: Path,
) -> None:
    promoter = MemoryPromoter(store=store, entries_dir=entries_dir)
    with pytest.raises(ValueError, match="not found"):
        promoter.promote(memory_id="never_existed", category="best_practice")


def test_promote_invalid_category_raises(
    store: MemoryStore, scope: MemoryScope, entries_dir: Path,
) -> None:
    record = store.save(
        scope=scope, type="project", name="x", description="x", body="x",
    )
    promoter = MemoryPromoter(store=store, entries_dir=entries_dir)
    with pytest.raises(ValueError, match="category"):
        promoter.promote(memory_id=record.id, category="lore")


def test_promote_invalid_severity_raises(
    store: MemoryStore, scope: MemoryScope, entries_dir: Path,
) -> None:
    record = store.save(
        scope=scope, type="project", name="x", description="x", body="x",
    )
    promoter = MemoryPromoter(store=store, entries_dir=entries_dir)
    with pytest.raises(ValueError, match="severity"):
        promoter.promote(
            memory_id=record.id, category="best_practice", severity="catastrophic",
        )


def test_promote_superseded_memory_raises(
    store: MemoryStore, scope: MemoryScope, entries_dir: Path,
) -> None:
    a = store.save(
        scope=scope, type="project", name="old", description="x", body="x",
    )
    b = store.save(
        scope=scope, type="project", name="new", description="x", body="x",
    )
    store.supersede(a.id, b.id)

    promoter = MemoryPromoter(store=store, entries_dir=entries_dir)
    with pytest.raises(ValueError, match="superseded"):
        promoter.promote(memory_id=a.id, category="best_practice")


def test_promote_refuses_to_clobber_existing_file(
    store: MemoryStore, scope: MemoryScope, entries_dir: Path,
) -> None:
    record = store.save(
        scope=scope, type="project", name="dupe", description="x", body="x",
    )
    promoter = MemoryPromoter(store=store, entries_dir=entries_dir)
    promoter.promote(memory_id=record.id, category="best_practice")
    with pytest.raises(FileExistsError):
        promoter.promote(memory_id=record.id, category="best_practice")


# ---------------------------------------------------------------------------
# Category coverage
# ---------------------------------------------------------------------------

def test_all_categories_writable(
    store: MemoryStore, scope: MemoryScope, entries_dir: Path,
) -> None:
    """Every category in KNOWLEDGE_CATEGORIES must promote without error."""
    promoter = MemoryPromoter(store=store, entries_dir=entries_dir)
    for cat in sorted(KNOWLEDGE_CATEGORIES):
        record = store.save(
            scope=scope, type="project", name=f"x-{cat}",
            description="generic platform knowledge", body="generic body",
        )
        result = promoter.promote(memory_id=record.id, category=cat)
        assert result.file.parent.name == cat
        assert result.file.exists()
