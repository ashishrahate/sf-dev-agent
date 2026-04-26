"""Unit tests for the knowledge base — frontmatter parsing, ingestion,
hash-gated re-embedding, semantic ranking, and tool wiring through
ToolRegistry. No live API calls; uses MockEmbedder throughout.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from sf_dev_agent.context import (
    KnowledgeBase,
    MockEmbedder,
    bundled_entries_dir,
)
from sf_dev_agent.context.knowledge.store import (
    _parse_frontmatter,
)
from sf_dev_agent.models.schemas import OrgConnection
from sf_dev_agent.tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def fixture_entries(tmp_path: Path) -> Path:
    """A small fixture knowledge base with three entries across categories."""
    base = tmp_path / "entries"
    _write(base / "governor_limits" / "soql-101.md",
           "---\n"
           "id: gl-soql-101\n"
           "title: SOQL queries 101 limit\n"
           "category: governor_limit\n"
           "severity: critical\n"
           "tags: [soql, governor_limit]\n"
           "references:\n"
           "  - https://example.com/limits\n"
           "---\n\n"
           "Apex transactions are capped at 100 SOQL queries.\n")
    _write(base / "anti_patterns" / "soql-in-loop.md",
           "---\n"
           "id: ap-soql-in-loop\n"
           "title: SOQL inside a loop\n"
           "category: anti_pattern\n"
           "severity: critical\n"
           "tags: [soql, bulkification]\n"
           "---\n\n"
           "Running SELECT in a for loop blows past the SOQL limit.\n")
    _write(base / "patterns" / "trigger-handler.md",
           "---\n"
           "id: pt-trigger-handler\n"
           "title: TriggerHandler base class\n"
           "category: pattern\n"
           "severity: info\n"
           "tags: [trigger, framework]\n"
           "---\n\n"
           "Use one trigger per object delegating to a handler class.\n")
    return base


@pytest.fixture
def kb(tmp_path: Path, fixture_entries: Path) -> KnowledgeBase:
    """KnowledgeBase pointed at the fixture entries, freshly loaded."""
    db = tmp_path / "knowledge.db"
    base = KnowledgeBase(db, entries_dir=fixture_entries)
    base.load_entries()
    yield base
    base.close()


@pytest.fixture
def org() -> OrgConnection:
    return OrgConnection(
        tenant_id="t1",
        org_alias="TestOrg",
        org_type="developer",
        instance_url="https://example.salesforce.com",
    )


# ---------------------------------------------------------------------------
# Frontmatter parser
# ---------------------------------------------------------------------------

def test_frontmatter_parses_inline_list_and_block_list() -> None:
    text = (
        "---\n"
        "id: x\n"
        "tags: [a, b, c]\n"
        "references:\n"
        "  - https://one\n"
        "  - https://two\n"
        "---\n\n"
        "Body here.\n"
    )
    fm, body = _parse_frontmatter(text)
    assert fm["id"] == "x"
    assert fm["tags"] == ["a", "b", "c"]
    assert fm["references"] == ["https://one", "https://two"]
    assert body.strip() == "Body here."


def test_frontmatter_handles_missing_block() -> None:
    text = "no frontmatter here, just body"
    fm, body = _parse_frontmatter(text)
    assert fm == {}
    assert body == text


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------

def test_load_entries_imports_three_categories(kb: KnowledgeBase) -> None:
    stats = kb.stats()
    assert stats == {"governor_limit": 1, "anti_pattern": 1, "pattern": 1}

    entry = kb.find_by_id("gl-soql-101")
    assert entry is not None
    assert entry.title == "SOQL queries 101 limit"
    assert entry.severity == "critical"
    assert "soql" in entry.tags
    assert entry.references == ["https://example.com/limits"]
    assert "100 SOQL queries" in entry.body


def test_load_entries_is_idempotent_when_unchanged(
    fixture_entries: Path, tmp_path: Path
) -> None:
    db = tmp_path / "kb.db"
    kb1 = KnowledgeBase(db, entries_dir=fixture_entries)
    first = kb1.load_entries()
    second = kb1.load_entries()
    kb1.close()

    assert first.loaded == 3
    assert first.updated == 0
    assert second.loaded == 0
    assert second.updated == 0
    assert second.skipped_unchanged == 3


def test_load_entries_detects_body_changes(
    fixture_entries: Path, tmp_path: Path
) -> None:
    db = tmp_path / "kb.db"
    kb1 = KnowledgeBase(db, entries_dir=fixture_entries)
    kb1.load_entries()
    kb1.close()

    # Edit one entry's body.
    target = fixture_entries / "anti_patterns" / "soql-in-loop.md"
    new = target.read_text() + "\nAdditional guidance: use a Map.\n"
    target.write_text(new, encoding="utf-8")

    kb2 = KnowledgeBase(db, entries_dir=fixture_entries)
    result = kb2.load_entries()
    kb2.close()

    assert result.updated == 1
    assert result.skipped_unchanged == 2


def test_auto_load_only_runs_when_table_empty(
    fixture_entries: Path, tmp_path: Path
) -> None:
    db = tmp_path / "kb.db"
    kb1 = KnowledgeBase(db, entries_dir=fixture_entries)
    first = kb1.auto_load_if_empty()
    second = kb1.auto_load_if_empty()
    kb1.close()

    assert first.loaded == 3
    # Second call should be a no-op — table no longer empty.
    assert second.loaded == 0
    assert second.skipped_unchanged == 0


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------

def test_embed_entries_populates_blobs(kb: KnowledgeBase) -> None:
    embedder = MockEmbedder(dim=64)
    result = kb.embed_entries(embedder)
    assert result.embedded == 3
    assert result.skipped_unchanged == 0

    coverage = kb.embedding_stats()
    for cat in ("governor_limit", "anti_pattern", "pattern"):
        assert coverage[cat]["total"] == 1
        assert coverage[cat]["embedded"] == 1


def test_embed_entries_is_hash_gated(kb: KnowledgeBase) -> None:
    embedder = MockEmbedder(dim=64)
    kb.embed_entries(embedder)
    second = kb.embed_entries(embedder)
    assert second.embedded == 0
    assert second.skipped_unchanged == 3


def test_embed_entries_force_re_embeds(kb: KnowledgeBase) -> None:
    embedder = MockEmbedder(dim=64)
    kb.embed_entries(embedder)
    forced = kb.embed_entries(embedder, force=True)
    assert forced.embedded == 3
    assert forced.skipped_unchanged == 0


def test_embed_entries_filters_by_category(kb: KnowledgeBase) -> None:
    embedder = MockEmbedder(dim=64)
    only_gov = kb.embed_entries(embedder, category="governor_limit")
    assert only_gov.embedded == 1
    assert only_gov.skipped_unchanged == 0

    coverage = kb.embedding_stats()
    assert coverage["governor_limit"]["embedded"] == 1
    assert coverage["anti_pattern"]["embedded"] == 0


# ---------------------------------------------------------------------------
# Semantic ranking
# ---------------------------------------------------------------------------

def test_search_ranks_by_topic(kb: KnowledgeBase) -> None:
    embedder = MockEmbedder(dim=64)
    kb.embed_entries(embedder)
    qv = embedder.embed_one("trigger handler framework class")
    hits = kb.search(qv, limit=3)
    assert hits[0].entry.id == "pt-trigger-handler", \
        f"Expected pt-trigger-handler first, got {[h.entry.id for h in hits]}"
    for prev, nxt in zip(hits, hits[1:]):
        assert prev.score >= nxt.score


def test_search_filters_by_category(kb: KnowledgeBase) -> None:
    embedder = MockEmbedder(dim=64)
    kb.embed_entries(embedder)
    qv = embedder.embed_one("anything")
    hits = kb.search(qv, category="governor_limit", limit=10)
    assert len(hits) == 1
    assert hits[0].entry.category == "governor_limit"


def test_search_returns_empty_when_no_embeddings(kb: KnowledgeBase) -> None:
    embedder = MockEmbedder(dim=64)
    qv = embedder.embed_one("anything")
    hits = kb.search(qv)
    assert hits == []


# ---------------------------------------------------------------------------
# Tool wiring through ToolRegistry
# ---------------------------------------------------------------------------

@pytest.fixture
def registry_with_kb(tmp_path: Path, fixture_entries: Path, org: OrgConnection):
    """ToolRegistry whose KB is pointed at the fixture entries dir.

    The store auto-loads from `bundled_entries_dir()` by default — we override
    by pre-loading the fixture before the registry gets used.
    """
    db = tmp_path / "registry_kb.db"
    kb = KnowledgeBase(db, entries_dir=fixture_entries)
    kb.load_entries()
    embedder = MockEmbedder(dim=64)
    kb.embed_entries(embedder)
    kb.close()

    return ToolRegistry(org=org, mock_org=False, index_db_path=db)


def test_knowledge_search_tool_returns_ranked_hits(
    registry_with_kb: ToolRegistry, monkeypatch
) -> None:
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    result = registry_with_kb.execute(
        "knowledge_search",
        {"query": "trigger handler framework", "limit": 3},
    )
    assert result["match_count"] >= 1
    assert result["results"][0]["id"] == "pt-trigger-handler"


def test_knowledge_search_filters_by_category(
    registry_with_kb: ToolRegistry, monkeypatch
) -> None:
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    result = registry_with_kb.execute(
        "knowledge_search",
        {"query": "anything", "category": "governor_limit"},
    )
    for hit in result["results"]:
        assert hit["category"] == "governor_limit"


def test_knowledge_search_min_score_threshold(
    registry_with_kb: ToolRegistry, monkeypatch
) -> None:
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    # An impossible threshold drops everything but exposes best_score_below.
    result = registry_with_kb.execute(
        "knowledge_search",
        {"query": "anything", "min_score": 0.99},
    )
    assert result["match_count"] == 0
    assert "best_score_below_threshold" in result


def test_embed_knowledge_base_tool_is_idempotent(
    registry_with_kb: ToolRegistry, monkeypatch
) -> None:
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    # Already embedded by the fixture — tool should hash-skip everything.
    result = registry_with_kb.execute("embed_knowledge_base", {})
    assert result["embedded"] == 0
    assert result["skipped_unchanged"] == 3


def test_knowledge_tools_are_registered(registry_with_kb: ToolRegistry) -> None:
    names = {t["name"] for t in registry_with_kb.get_tool_definitions()}
    assert {"knowledge_search", "embed_knowledge_base"}.issubset(names)


def test_knowledge_tools_mocked_in_mock_org_mode(
    tmp_path: Path, org: OrgConnection
) -> None:
    registry = ToolRegistry(org=org, mock_org=True, index_db_path=tmp_path / "x.db")
    embed = registry.execute("embed_knowledge_base", {})
    search = registry.execute("knowledge_search", {"query": "foo"})
    assert embed.get("mocked") is True
    assert search.get("mocked") is True


# ---------------------------------------------------------------------------
# Bundled entries — sanity check the shipped content actually loads
# ---------------------------------------------------------------------------

def test_bundled_entries_directory_exists() -> None:
    d = bundled_entries_dir()
    assert d.exists()
    assert d.is_dir()
    md_files = list(d.rglob("*.md"))
    assert len(md_files) >= 25, f"Expected ~30 bundled entries, found {len(md_files)}"


def test_bundled_entries_load_without_parse_errors(tmp_path: Path) -> None:
    """All shipped entries should have valid frontmatter."""
    db = tmp_path / "bundled.db"
    kb_inst = KnowledgeBase(db)  # uses bundled_entries_dir() by default
    result = kb_inst.load_entries()
    kb_inst.close()
    assert result.parse_errors == [], \
        f"Bundled entries failed to parse: {result.parse_errors}"
    assert result.loaded >= 25
