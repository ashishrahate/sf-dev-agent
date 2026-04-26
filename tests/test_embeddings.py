"""Unit tests for the embedding pipeline.

Uses MockEmbedder throughout — no live API calls. The mock is deterministic
(SHA-256-derived bag-of-words) which is enough to verify ranking correctness.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from sf_dev_agent.context import (
    MetadataIndex,
    MockEmbedder,
    ingest_directory,
)
from sf_dev_agent.context.embedders.base import hash_text
from sf_dev_agent.models.schemas import OrgConnection
from sf_dev_agent.tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# Fixture index
# ---------------------------------------------------------------------------

def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def fixture_tree(tmp_path: Path) -> Path:
    base = tmp_path / "force-app" / "main" / "default"
    _write(base / "classes" / "AccountDeduplicator.cls",
           "public class AccountDeduplicator {\n"
           "    // Detects duplicate accounts based on matching email and phone.\n"
           "    public static void dedupAccounts(List<Account> accounts) {}\n"
           "}\n")
    _write(base / "classes" / "InvoiceTaxCalculator.cls",
           "public class InvoiceTaxCalculator {\n"
           "    // Computes tax for invoices using regional tax rules.\n"
           "    public static Decimal calculateTax(Decimal amount) { return 0; }\n"
           "}\n")
    _write(base / "classes" / "LeadRouter.cls",
           "public class LeadRouter {\n"
           "    // Assigns leads to sales reps based on territory.\n"
           "    public static User route(Lead l) { return null; }\n"
           "}\n")
    return tmp_path


@pytest.fixture
def index_db(fixture_tree: Path, tmp_path: Path) -> Path:
    db_path = tmp_path / "embed_test.db"
    result = ingest_directory(source_dir=fixture_tree, db_path=db_path)
    assert result.success
    return db_path


# ---------------------------------------------------------------------------
# Embedder
# ---------------------------------------------------------------------------

def test_mock_embedder_is_deterministic() -> None:
    e = MockEmbedder(dim=64)
    a = e.embed_one("public class Foo {}")
    b = e.embed_one("public class Foo {}")
    np.testing.assert_array_equal(a, b)


def test_mock_embedder_different_texts_different_vectors() -> None:
    e = MockEmbedder(dim=64)
    a = e.embed_one("public class Foo {}")
    b = e.embed_one("public class Bar {}")
    assert not np.array_equal(a, b)


def test_mock_embedder_returns_correct_shape() -> None:
    e = MockEmbedder(dim=128)
    vectors = e.embed(["one", "two", "three"])
    assert len(vectors) == 3
    for v in vectors:
        assert v.shape == (128,)
        assert v.dtype == np.float32


def test_mock_embedder_normalizes_output() -> None:
    """Cosine sim collapses to dot product when both sides are normalized."""
    e = MockEmbedder(dim=64)
    v = e.embed_one("the quick brown fox jumps over the lazy dog")
    norm = float(np.linalg.norm(v))
    assert pytest.approx(norm, abs=1e-5) == 1.0


# ---------------------------------------------------------------------------
# embed_components — index-side
# ---------------------------------------------------------------------------

def test_embed_components_populates_blobs(index_db: Path) -> None:
    embedder = MockEmbedder(dim=64)
    with MetadataIndex(index_db) as index:
        result = index.embed_components(embedder)

    assert result.embedded == 3
    assert result.skipped_unchanged == 0
    assert result.errors == []

    with MetadataIndex(index_db) as index:
        for c in index.find_by_type("ApexClass"):
            row = index._conn.execute(
                "SELECT embedding, embedded_source_hash FROM components WHERE id = ?",
                (c.id,),
            ).fetchone()
            assert row["embedding"] is not None
            assert row["embedded_source_hash"] is not None
            assert len(row["embedded_source_hash"]) == 64  # sha256 hex


def test_embed_components_is_hash_gated(index_db: Path) -> None:
    """A second run with the same source should re-embed nothing."""
    embedder = MockEmbedder(dim=64)
    with MetadataIndex(index_db) as index:
        first = index.embed_components(embedder)
        second = index.embed_components(embedder)

    assert first.embedded == 3
    assert second.embedded == 0
    assert second.skipped_unchanged == 3


def test_embed_components_only_re_embeds_changed_rows(
    fixture_tree: Path, index_db: Path
) -> None:
    """Modify one source file, re-ingest, then re-embed — only that row should re-embed."""
    embedder = MockEmbedder(dim=64)
    with MetadataIndex(index_db) as index:
        index.embed_components(embedder)

    # Mutate AccountDeduplicator.
    cls_path = (
        fixture_tree / "force-app" / "main" / "default"
        / "classes" / "AccountDeduplicator.cls"
    )
    cls_path.write_text(
        cls_path.read_text() + "\n// added comment, source changed\n",
        encoding="utf-8",
    )
    ingest_directory(source_dir=fixture_tree, db_path=index_db)

    with MetadataIndex(index_db) as index:
        result = index.embed_components(embedder)

    assert result.embedded == 1, \
        f"Only the mutated class should re-embed, got {result.embedded}"
    assert result.skipped_unchanged == 2


def test_embed_components_force_re_embeds_everything(index_db: Path) -> None:
    embedder = MockEmbedder(dim=64)
    with MetadataIndex(index_db) as index:
        index.embed_components(embedder)
        forced = index.embed_components(embedder, force=True)
    assert forced.embedded == 3
    assert forced.skipped_unchanged == 0


def test_clear_embeddings(index_db: Path) -> None:
    embedder = MockEmbedder(dim=64)
    with MetadataIndex(index_db) as index:
        index.embed_components(embedder)
        cleared = index.clear_embeddings()
        assert cleared == 3
        stats = index.embedding_stats()
        for type_stats in stats.values():
            assert type_stats["embedded"] == 0


# ---------------------------------------------------------------------------
# semantic_search — ranking correctness
# ---------------------------------------------------------------------------

def test_semantic_search_ranks_by_topic(index_db: Path) -> None:
    """A query about 'duplicate detection' should rank the dedup class first."""
    embedder = MockEmbedder(dim=64)
    with MetadataIndex(index_db) as index:
        index.embed_components(embedder)
        query_vec = embedder.embed_one("duplicate detection accounts")
        hits = index.semantic_search(query_vec, limit=3)

    assert len(hits) == 3
    assert hits[0].component.api_name == "AccountDeduplicator", \
        f"Expected AccountDeduplicator first, got: {[h.component.api_name for h in hits]}"
    # Scores should be sorted descending.
    for prev, nxt in zip(hits, hits[1:]):
        assert prev.score >= nxt.score


def test_semantic_search_returns_empty_when_no_embeddings(index_db: Path) -> None:
    embedder = MockEmbedder(dim=64)
    with MetadataIndex(index_db) as index:
        # No embed_components call — DB has no embeddings yet.
        query_vec = embedder.embed_one("anything")
        hits = index.semantic_search(query_vec)
    assert hits == []


def test_semantic_search_respects_component_type_filter(index_db: Path) -> None:
    embedder = MockEmbedder(dim=64)
    with MetadataIndex(index_db) as index:
        index.embed_components(embedder)
        query_vec = embedder.embed_one("anything")
        hits = index.semantic_search(query_vec, component_type="ApexTrigger")
    assert hits == []  # fixture has no triggers


# ---------------------------------------------------------------------------
# Tool wiring — through ToolRegistry
# ---------------------------------------------------------------------------

@pytest.fixture
def registry_with_embeddings(index_db: Path) -> ToolRegistry:
    """Build the index, embed everything via Mock, return a ToolRegistry."""
    embedder = MockEmbedder(dim=64)
    with MetadataIndex(index_db) as index:
        index.embed_components(embedder)

    org = OrgConnection(
        tenant_id="t1",
        org_alias="TestOrg",
        org_type="developer",
        instance_url="https://example.salesforce.com",
    )
    return ToolRegistry(org=org, mock_org=False, index_db_path=index_db)


def test_semantic_search_tool_returns_ranked_hits(registry_with_embeddings: ToolRegistry, monkeypatch) -> None:
    # Force the registry to use MockEmbedder by hiding the Google API key.
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    result = registry_with_embeddings.execute(
        "semantic_search",
        {"query": "duplicate detection accounts", "limit": 3},
    )
    assert result["match_count"] == 3
    assert result["results"][0]["api_name"] == "AccountDeduplicator"
    # Each result should carry a score; first should be the highest.
    scores = [r["score"] for r in result["results"]]
    assert scores == sorted(scores, reverse=True)


def test_embed_metadata_index_tool_is_idempotent(registry_with_embeddings: ToolRegistry, monkeypatch) -> None:
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    result = registry_with_embeddings.execute("embed_metadata_index", {})
    # Already embedded by the fixture — second pass should skip everything.
    assert result["embedded"] == 0
    assert result["skipped_unchanged"] == 3


def test_semantic_search_tool_returns_error_when_index_missing(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    org = OrgConnection(
        tenant_id="t1",
        org_alias="TestOrg",
        org_type="developer",
        instance_url="https://example.salesforce.com",
    )
    registry = ToolRegistry(
        org=org, mock_org=False,
        index_db_path=tmp_path / "no_such.db",
    )
    result = registry.execute("semantic_search", {"query": "anything"})
    assert "error" in result


def test_new_vector_tools_are_registered(registry_with_embeddings: ToolRegistry) -> None:
    names = {t["name"] for t in registry_with_embeddings.get_tool_definitions()}
    assert {"semantic_search", "embed_metadata_index"}.issubset(names)


def test_semantic_search_mocked_in_mock_org_mode(tmp_path: Path) -> None:
    org = OrgConnection(
        tenant_id="t1",
        org_alias="TestOrg",
        org_type="developer",
        instance_url="https://example.salesforce.com",
    )
    registry = ToolRegistry(org=org, mock_org=True, index_db_path=tmp_path / "x.db")
    result = registry.execute("semantic_search", {"query": "anything"})
    assert result.get("mocked") is True
    assert result["match_count"] == 0


# ---------------------------------------------------------------------------
# Hash helper sanity
# ---------------------------------------------------------------------------

def test_hash_text_is_deterministic_and_64_chars() -> None:
    h1 = hash_text("hello world")
    h2 = hash_text("hello world")
    assert h1 == h2
    assert len(h1) == 64
    assert hash_text("hello world ") != h1
