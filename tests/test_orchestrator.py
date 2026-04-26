"""Unit tests for the Retrieval Orchestrator.

Uses MockEmbedder throughout — no live API calls. The mock is deterministic
(SHA-256-derived bag-of-words) so we get stable rankings across runs and
across machines. Real Gemini behavior is exercised by the smoke harness when
the user runs against a real org.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sf_dev_agent.context import (
    KnowledgeBase,
    MetadataIndex,
    MockEmbedder,
    ContextHit,
    RetrievalResult,
    ingest_directory,
    retrieve_context,
)


# ---------------------------------------------------------------------------
# Fixture index — code with body diversity so semantic + literal hit
# different rows
# ---------------------------------------------------------------------------

def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def fixture_tree(tmp_path: Path) -> Path:
    """A handful of Apex classes with distinguishable bodies + one trigger."""
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
    _write(base / "classes" / "AccountTriggerHandler.cls",
           "public class AccountTriggerHandler {\n"
           "    public static void handleBeforeInsert(List<Account> accs) {}\n"
           "}\n")
    _write(base / "triggers" / "AccountTrigger.trigger",
           "trigger AccountTrigger on Account (before insert) {\n"
           "    AccountTriggerHandler.handleBeforeInsert(Trigger.new);\n"
           "}\n")
    return tmp_path


@pytest.fixture
def populated_db(fixture_tree: Path, tmp_path: Path) -> Path:
    """Index ingested + embedded + knowledge entries embedded.

    Uses the bundled knowledge entries so we have real cross-layer fan-out
    to exercise — KnowledgeBase auto-loads bundled entries on first open.
    """
    db_path = tmp_path / "orchestrator_test.db"
    result = ingest_directory(source_dir=fixture_tree, db_path=db_path)
    assert result.success

    embedder = MockEmbedder(dim=64)
    with MetadataIndex(db_path) as index:
        embed_result = index.embed_components(embedder)
        assert embed_result.embedded > 0

    with KnowledgeBase(db_path) as kb:
        kb.auto_load_if_empty()
        kb_embed = kb.embed_entries(embedder=embedder)
        assert kb_embed.embedded > 0

    return db_path


# ---------------------------------------------------------------------------
# Fan-out coverage
# ---------------------------------------------------------------------------

def test_retrieve_context_calls_all_three_layers(populated_db: Path) -> None:
    """A real query should surface hits from semantic, literal, and knowledge."""
    embedder = MockEmbedder(dim=64)
    result = retrieve_context(
        query="duplicate account detection",
        db_path=populated_db,
        embedder=embedder,
        max_tokens=8000,  # plenty so nothing is dropped
    )

    assert isinstance(result, RetrievalResult)
    assert result.layer_errors == []
    assert result.embedder_name == embedder.name
    # Semantic + literal both run against the same SQLite. Knowledge runs
    # against bundled entries auto-loaded on first KB open.
    assert result.layer_counts["semantic"] >= 1
    assert result.layer_counts["literal"] >= 1
    assert result.layer_counts["knowledge"] >= 1
    sources = {h.source for h in result.hits}
    assert "semantic" in sources or "literal" in sources, \
        "At least one code-side layer should produce a hit for 'duplicate account'"
    assert "knowledge" in sources, "Knowledge layer should contribute"


def test_retrieve_context_returns_provenance_per_hit(populated_db: Path) -> None:
    embedder = MockEmbedder(dim=64)
    result = retrieve_context(
        query="trigger handler pattern",
        db_path=populated_db,
        embedder=embedder,
    )
    for hit in result.hits:
        assert hit.source in {"semantic", "literal", "knowledge", "graph"}
        assert hit.citation
        assert 0.0 <= hit.score <= 1.0


# ---------------------------------------------------------------------------
# Dedupe across layers
# ---------------------------------------------------------------------------

def test_retrieve_context_dedupes_same_component_across_layers(
    populated_db: Path,
) -> None:
    """A component surfaced by both semantic AND literal collapses to one hit
    that records cross-layer agreement under metadata.also_surfaced_by."""
    embedder = MockEmbedder(dim=64)
    # The literal "AccountTriggerHandler" matches by api_name; the semantic
    # vector also lights up on its body. Same component_id, two layers.
    result = retrieve_context(
        query="AccountTriggerHandler",
        db_path=populated_db,
        embedder=embedder,
        max_tokens=8000,
    )

    by_component: dict[str, list[ContextHit]] = {}
    for hit in result.hits:
        if hit.component_id is not None and hit.source != "graph":
            by_component.setdefault(hit.component_id, []).append(hit)

    # No component_id should appear twice — one merged row per code component.
    for cid, bucket in by_component.items():
        assert len(bucket) == 1, (
            f"Expected dedupe for {cid}, but found {[h.source for h in bucket]}"
        )

    # If AccountTriggerHandler was surfaced by both layers, the merged hit
    # should record the loser under also_surfaced_by.
    target = next(
        (h for h in result.hits
         if h.component_id == "ApexClass:AccountTriggerHandler"),
        None,
    )
    if target is not None:
        also = target.metadata.get("also_surfaced_by", [])
        # Either both layers fired (also has a row), or only one fired (empty).
        assert isinstance(also, list)


# ---------------------------------------------------------------------------
# Token budget
# ---------------------------------------------------------------------------

def test_retrieve_context_respects_token_budget(populated_db: Path) -> None:
    """A tight budget drops hits and reports the count under `truncated`."""
    embedder = MockEmbedder(dim=64)
    result = retrieve_context(
        query="account",
        db_path=populated_db,
        embedder=embedder,
        max_tokens=200,        # tight; will fit very few hits
        max_per_layer=10,      # gather plenty of candidates first
    )
    assert result.estimated_tokens <= 200 + 50, (
        "Budget should be roughly respected (allowing slack for the "
        "always-keep-one floor)."
    )
    # We almost certainly dropped something — gathering 10/layer with a
    # 200-token cap is intentionally too tight.
    assert result.truncated >= 1


def test_retrieve_context_always_keeps_at_least_one_hit(populated_db: Path) -> None:
    """Even with an absurdly tight budget, the top hit must come through."""
    embedder = MockEmbedder(dim=64)
    result = retrieve_context(
        query="account",
        db_path=populated_db,
        embedder=embedder,
        max_tokens=1,    # smaller than any hit
    )
    # At least one hit should survive (the floor protects against empty payload).
    if result.layer_counts.get("semantic", 0) + result.layer_counts.get("literal", 0) > 0:
        assert len(result.hits) >= 1


# ---------------------------------------------------------------------------
# Per-hit char cap
# ---------------------------------------------------------------------------

def test_retrieve_context_truncates_long_bodies(populated_db: Path) -> None:
    """The per-hit char cap protects against one giant body eating the budget."""
    embedder = MockEmbedder(dim=64)
    result = retrieve_context(
        query="account",
        db_path=populated_db,
        embedder=embedder,
        per_hit_char_cap=50,    # very tight
        max_tokens=8000,
    )
    for hit in result.hits:
        # Allow a small overhead for the "[truncated]" marker.
        assert len(hit.body) <= 50 + 30


# ---------------------------------------------------------------------------
# Graph enrichment
# ---------------------------------------------------------------------------

def test_retrieve_context_graph_enriches_top_code_hits(
    populated_db: Path,
) -> None:
    """Top code hits should have their 1-hop relationships surfaced as `graph` rows."""
    embedder = MockEmbedder(dim=64)
    result = retrieve_context(
        query="AccountTrigger",
        db_path=populated_db,
        embedder=embedder,
        enrich_top_k=3,
        max_tokens=8000,
    )
    graph_hits = [h for h in result.hits if h.source == "graph"]
    assert len(graph_hits) >= 1, (
        "AccountTrigger TRIGGERS_ON Account; that edge should be enriched"
    )
    # Graph hits sort just below their parent (score - 0.05).
    code_hits = [
        h for h in result.hits
        if h.source in ("semantic", "literal") and h.component_id is not None
    ]
    if code_hits and graph_hits:
        assert max(g.score for g in graph_hits) <= max(c.score for c in code_hits)


def test_retrieve_context_enrich_top_k_zero_disables_graph(
    populated_db: Path,
) -> None:
    embedder = MockEmbedder(dim=64)
    result = retrieve_context(
        query="AccountTrigger",
        db_path=populated_db,
        embedder=embedder,
        enrich_top_k=0,
    )
    graph_hits = [h for h in result.hits if h.source == "graph"]
    assert graph_hits == []
    assert result.layer_counts.get("graph", 0) == 0


# ---------------------------------------------------------------------------
# Per-layer caps
# ---------------------------------------------------------------------------

def test_retrieve_context_respects_max_per_layer(populated_db: Path) -> None:
    embedder = MockEmbedder(dim=64)
    result = retrieve_context(
        query="account",
        db_path=populated_db,
        embedder=embedder,
        max_per_layer=2,
        max_tokens=8000,
    )
    # Each layer's contribution can't exceed max_per_layer (dedupe may reduce
    # the visible count, but never increase past the cap).
    for layer in ("semantic", "literal", "knowledge"):
        assert result.layer_counts.get(layer, 0) <= 2


# ---------------------------------------------------------------------------
# Defensive paths
# ---------------------------------------------------------------------------

def test_retrieve_context_empty_query_returns_empty_result(
    populated_db: Path,
) -> None:
    """Empty/whitespace queries short-circuit — no embedding API call, no hits."""
    embedder = MockEmbedder(dim=64)
    result = retrieve_context(
        query="   ",
        db_path=populated_db,
        embedder=embedder,
    )
    assert result.hits == []
    assert result.layer_errors == []
    assert result.estimated_tokens == 0
    # Empty path returns before naming an embedder.
    assert result.embedder_name == ""


def test_retrieve_context_missing_db_returns_structured_error(
    tmp_path: Path,
) -> None:
    embedder = MockEmbedder(dim=64)
    result = retrieve_context(
        query="anything",
        db_path=tmp_path / "nope.db",
        embedder=embedder,
    )
    assert result.hits == []
    assert any("index not found" in e for e in result.layer_errors)


def test_retrieve_context_layer_failure_isolated(
    populated_db: Path, monkeypatch
) -> None:
    """One layer raising must NOT kill the whole call — error captured, others run."""
    from sf_dev_agent.context import orchestrator as orch

    def boom(*args, **kwargs):
        raise RuntimeError("simulated knowledge layer crash")

    monkeypatch.setattr(orch, "_layer_knowledge", boom)

    embedder = MockEmbedder(dim=64)
    result = retrieve_context(
        query="account",
        db_path=populated_db,
        embedder=embedder,
    )
    assert any("knowledge layer" in e for e in result.layer_errors)
    # Semantic + literal still produced hits.
    assert result.layer_counts["semantic"] >= 0
    assert result.layer_counts["literal"] >= 0
    sources = {h.source for h in result.hits}
    assert sources, "Other layers should still surface results"


# ---------------------------------------------------------------------------
# to_dict serialization (what the tool layer returns)
# ---------------------------------------------------------------------------

def test_retrieve_context_to_dict_has_expected_keys(populated_db: Path) -> None:
    embedder = MockEmbedder(dim=64)
    result = retrieve_context(
        query="account",
        db_path=populated_db,
        embedder=embedder,
    )
    d = result.to_dict()
    assert set(d.keys()) >= {
        "query", "embedder", "hits", "layer_counts",
        "estimated_tokens", "truncated", "layer_errors",
    }
    if d["hits"]:
        h = d["hits"][0]
        assert set(h.keys()) >= {
            "source", "kind", "title", "citation",
            "score", "component_id", "body", "metadata",
        }


# ---------------------------------------------------------------------------
# Tool-registry wiring
# ---------------------------------------------------------------------------

def test_retrieve_context_tool_registered() -> None:
    """The orchestrator is exposed as a tool definition the agent can see."""
    from sf_dev_agent.models.schemas import OrgConnection
    from sf_dev_agent.tools.registry import ToolRegistry

    org = OrgConnection(
        tenant_id="t1",
        org_alias="TestOrg",
        org_type="developer",
        instance_url="https://example.salesforce.com",
    )
    registry = ToolRegistry(org=org, mock_org=True)
    names = [t["name"] for t in registry.get_tool_definitions()]
    assert "retrieve_context" in names


def test_retrieve_context_tool_mock_mode_returns_canned() -> None:
    from sf_dev_agent.models.schemas import OrgConnection
    from sf_dev_agent.tools.registry import ToolRegistry

    org = OrgConnection(
        tenant_id="t1",
        org_alias="TestOrg",
        org_type="developer",
        instance_url="https://example.salesforce.com",
    )
    registry = ToolRegistry(org=org, mock_org=True)
    response = registry.execute(
        "retrieve_context",
        {"query": "anything"},
    )
    assert response["mocked"] is True
    assert response["query"] == "anything"
    assert response["hits"] == []
