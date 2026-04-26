"""Retrieval Orchestrator — single entry point for cross-layer context.

The hybrid context engine has three layers (each with its own tool):

    Layer 1: vector store    -> semantic_search   (cosine over component embeddings)
    Layer 2: metadata index  -> code_search       (substring) + sf_dependency_graph
    Layer 3: knowledge base  -> knowledge_search  (cosine over curated entries)

Letting the agent itself orchestrate works, but every per-layer call is a
separate LLM round-trip and the agent can simply forget to consult a layer.
This module is the *programmatic* orchestrator the project summary called for:
fan out to all three layers from a single query, dedupe + rank the merged
hits, optionally enrich top code hits with a one-hop graph walk, and truncate
to a token budget so the assembled payload stays focused.

Public API:
    retrieve_context(query, ...) -> RetrievalResult
        Run the full fan-out and return a structured payload.

    ContextHit
        One result row, tagged by which layer surfaced it.

    RetrievalResult
        The composed payload + provenance + budget metadata.

Layer 1 and Layer 3 each cost one Gemini embedding API call for the query,
not two — they share the embedding (Gemini's RETRIEVAL_QUERY task type is the
same for both). Layer 2's literal search is offline. Graph enrichment is
also offline. A typical call costs exactly one remote embedding.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sf_dev_agent.context.embedders import Embedder, create_embedder
from sf_dev_agent.context.index import ComponentRow, MetadataIndex
from sf_dev_agent.context.knowledge import KnowledgeBase

logger = logging.getLogger(__name__)


# Approximate chars per token; good enough for budget gating without pulling
# in tiktoken/SentencePiece. Code averages ~4 chars/token, prose ~3.5; we use
# the slightly conservative 4 so we err on under-shooting the LLM's context.
CHARS_PER_TOKEN = 4

# Per-hit body cap so one giant Apex class doesn't eat the whole budget. The
# orchestrator's job is breadth — pulling N short witnesses, not one long one.
DEFAULT_PER_HIT_CHAR_CAP = 3200  # ~800 tokens


@dataclass
class ContextHit:
    """One row in the assembled context payload.

    `source` records which layer surfaced the hit so the agent (and humans
    debugging it) can see provenance. `score` is normalized to [0, 1]:
    cosine sim from semantic/knowledge layers passes through; literal hits
    get a fixed mid-band score that's lifted on exact-name match. Graph
    edges get the parent hit's score minus a small penalty.
    """

    source: str               # "semantic" | "literal" | "knowledge" | "graph"
    kind: str                 # ComponentType, "knowledge_entry", or relationship_type
    title: str                # human-friendly identifier (api_name or knowledge title)
    body: str                 # the content already truncated to fit budget
    score: float              # blended/normalized [0, 1]
    component_id: str | None  # set for code hits + graph edges; None for knowledge
    citation: str             # short provenance label, e.g. "ApexClass:AccountTrigger"
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def estimated_tokens(self) -> int:
        """Rough char-based token estimate for budget tracking."""
        return max(1, (len(self.title) + len(self.body)) // CHARS_PER_TOKEN)


@dataclass
class RetrievalResult:
    """Output of a single retrieve_context call."""

    query: str
    hits: list[ContextHit] = field(default_factory=list)
    layer_counts: dict[str, int] = field(default_factory=dict)
    estimated_tokens: int = 0
    truncated: int = 0          # hits dropped because the budget was full
    layer_errors: list[str] = field(default_factory=list)
    embedder_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Tool-friendly serialization."""
        return {
            "query": self.query,
            "embedder": self.embedder_name,
            "hits": [_hit_to_dict(h) for h in self.hits],
            "layer_counts": dict(self.layer_counts),
            "estimated_tokens": self.estimated_tokens,
            "truncated": self.truncated,
            "layer_errors": list(self.layer_errors),
        }


def _hit_to_dict(hit: ContextHit) -> dict[str, Any]:
    return {
        "source": hit.source,
        "kind": hit.kind,
        "title": hit.title,
        "citation": hit.citation,
        "score": round(hit.score, 4),
        "component_id": hit.component_id,
        "body": hit.body,
        "metadata": hit.metadata,
    }


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------

def retrieve_context(
    query: str,
    db_path: Path | str | None = None,
    embedder: Embedder | None = None,
    max_tokens: int = 4000,
    max_per_layer: int = 6,
    enrich_top_k: int = 3,
    component_type: str | None = None,
    per_hit_char_cap: int = DEFAULT_PER_HIT_CHAR_CAP,
    knowledge_category: str | None = None,
) -> RetrievalResult:
    """Compose a focused context payload from all three layers.

    The query is embedded once (Gemini RETRIEVAL_QUERY) and reused for the
    semantic + knowledge layers. The literal layer needs no embedding. Graph
    enrichment runs against the metadata index; it's offline.

    Args:
        query: natural-language description of what to gather context for.
        db_path: SQLite index path (defaults to package default).
        embedder: pre-built embedder; defaults to env-resolved (Gemini if
            GOOGLE_API_KEY is set, else mock). Tests inject MockEmbedder.
        max_tokens: total budget for the assembled payload.
        max_per_layer: cap per layer before dedupe; prevents one layer from
            hogging the working set.
        enrich_top_k: how many of the top code hits to graph-enrich (1-hop).
        component_type: restrict semantic + literal to this type.
        per_hit_char_cap: each hit's body is truncated to at most this many
            chars before the budget walk.
        knowledge_category: restrict knowledge layer to this category.

    Returns:
        RetrievalResult with hits sorted by score desc, totals, and any
        per-layer errors that didn't kill the run.
    """
    result = RetrievalResult(query=query)

    if not query or not query.strip():
        return result

    db_path = Path(db_path) if db_path else _default_db_path()
    if not db_path.exists():
        result.layer_errors.append(
            f"index not found at {db_path}; run build_metadata_index first"
        )
        return result

    # One embedding for the query, reused across vector layers.
    query_embedding = None
    if embedder is None:
        try:
            embedder = _make_query_embedder()
        except (ValueError, ImportError) as exc:
            result.layer_errors.append(
                f"could not initialize embedder: {type(exc).__name__}: {exc}"
            )
            embedder = None

    if embedder is not None:
        result.embedder_name = embedder.name
        try:
            query_embedding = embedder.embed_one(query)
        except Exception as exc:
            result.layer_errors.append(
                f"embedding query failed: {type(exc).__name__}: {exc}"
            )
            query_embedding = None

    candidates: list[ContextHit] = []

    # --- Layer 1: semantic search over the metadata index --------------
    if query_embedding is not None:
        try:
            sem_hits = _layer_semantic(
                db_path=db_path,
                query_embedding=query_embedding,
                component_type=component_type,
                limit=max_per_layer,
                char_cap=per_hit_char_cap,
            )
            candidates.extend(sem_hits)
            result.layer_counts["semantic"] = len(sem_hits)
        except Exception as exc:
            result.layer_errors.append(
                f"semantic layer: {type(exc).__name__}: {exc}"
            )
            result.layer_counts["semantic"] = 0
    else:
        # No embedder -> the two vector layers are unavailable; keep going.
        result.layer_counts["semantic"] = 0

    # --- Layer 2: literal substring search -----------------------------
    try:
        lit_hits = _layer_literal(
            db_path=db_path,
            query=query,
            component_type=component_type,
            limit=max_per_layer,
            char_cap=per_hit_char_cap,
        )
        candidates.extend(lit_hits)
        result.layer_counts["literal"] = len(lit_hits)
    except Exception as exc:
        result.layer_errors.append(f"literal layer: {type(exc).__name__}: {exc}")
        result.layer_counts["literal"] = 0

    # --- Layer 3: knowledge base ---------------------------------------
    if query_embedding is not None:
        try:
            know_hits = _layer_knowledge(
                db_path=db_path,
                query_embedding=query_embedding,
                category=knowledge_category,
                limit=max_per_layer,
                char_cap=per_hit_char_cap,
            )
            candidates.extend(know_hits)
            result.layer_counts["knowledge"] = len(know_hits)
        except Exception as exc:
            result.layer_errors.append(
                f"knowledge layer: {type(exc).__name__}: {exc}"
            )
            result.layer_counts["knowledge"] = 0
    else:
        result.layer_counts["knowledge"] = 0

    # --- Dedupe code hits across semantic + literal --------------------
    deduped = _dedupe_by_component(candidates)

    # --- Graph enrichment on top-K code hits ---------------------------
    enrichment: list[ContextHit] = []
    if enrich_top_k > 0:
        try:
            enrichment = _graph_enrich(
                db_path=db_path,
                hits=deduped,
                top_k=enrich_top_k,
                char_cap=per_hit_char_cap,
            )
        except Exception as exc:
            result.layer_errors.append(f"graph enrichment: {type(exc).__name__}: {exc}")
            enrichment = []

    result.layer_counts["graph"] = len(enrichment)
    deduped.extend(enrichment)

    # Sort and apply the token budget greedily.
    deduped.sort(key=lambda h: h.score, reverse=True)
    fit, dropped, used = _fit_to_budget(deduped, max_tokens)
    result.hits = fit
    result.truncated = dropped
    result.estimated_tokens = used
    return result


# ---------------------------------------------------------------------------
# Per-layer adapters
# ---------------------------------------------------------------------------

def _layer_semantic(
    *,
    db_path: Path,
    query_embedding,
    component_type: str | None,
    limit: int,
    char_cap: int,
) -> list[ContextHit]:
    with MetadataIndex(db_path) as index:
        hits = index.semantic_search(
            query_embedding=query_embedding,
            component_type=component_type,
            limit=limit,
        )
    out: list[ContextHit] = []
    for hit in hits:
        out.append(_component_to_hit(
            comp=hit.component,
            source="semantic",
            score=_clip01(float(hit.score)),
            char_cap=char_cap,
        ))
    return out


def _layer_literal(
    *,
    db_path: Path,
    query: str,
    component_type: str | None,
    limit: int,
    char_cap: int,
) -> list[ContextHit]:
    """Substring search with per-token fallback for multi-word queries.

    The underlying `MetadataIndex.search` does a `LIKE '%query%'`, so a
    multi-word query like "duplicate account detection" never matches
    anything unless that exact phrase appears in the source. For the
    orchestrator's "user doesn't have to think about layers" contract, we
    first try the full phrase (preserves precedence for exact matches), then
    fall back to per-token searches and merge.
    """
    needle = query.strip().lower()
    tokens = [t for t in needle.split() if len(t) >= 3]

    seen_ids: set[str] = set()
    out: list[ContextHit] = []

    with MetadataIndex(db_path) as index:
        # Pass 1: full-phrase match (highest confidence — phrase IS in source).
        for comp in index.search(query, component_type=component_type, limit=limit):
            if comp.id in seen_ids:
                continue
            seen_ids.add(comp.id)
            # Lift on exact-name match; otherwise the phrase appears verbatim
            # somewhere — strong signal but not as strong as an exact name.
            score = 0.75 if comp.api_name.lower() == needle else 0.6
            out.append(_component_to_hit(
                comp=comp, source="literal", score=score, char_cap=char_cap,
            ))
            if len(out) >= limit:
                return out

        # Pass 2: per-token fallback, weaker score (one of N words appeared).
        for token in tokens:
            if len(out) >= limit:
                break
            for comp in index.search(token, component_type=component_type, limit=limit):
                if comp.id in seen_ids:
                    continue
                seen_ids.add(comp.id)
                out.append(_component_to_hit(
                    comp=comp, source="literal", score=0.5, char_cap=char_cap,
                ))
                if len(out) >= limit:
                    break

    return out


def _layer_knowledge(
    *,
    db_path: Path,
    query_embedding,
    category: str | None,
    limit: int,
    char_cap: int,
) -> list[ContextHit]:
    with KnowledgeBase(db_path) as kb:
        hits = kb.search(
            query_embedding=query_embedding, category=category, limit=limit,
        )
    out: list[ContextHit] = []
    for hit in hits:
        entry = hit.entry
        body = _truncate(entry.body, char_cap)
        out.append(ContextHit(
            source="knowledge",
            kind="knowledge_entry",
            title=entry.title,
            body=body,
            score=_clip01(float(hit.score)),
            component_id=None,
            citation=f"knowledge:{entry.category}/{entry.id}",
            metadata={
                "id": entry.id,
                "category": entry.category,
                "severity": entry.severity,
                "tags": list(entry.tags),
                "references": list(entry.references),
            },
        ))
    return out


def _graph_enrich(
    *,
    db_path: Path,
    hits: list[ContextHit],
    top_k: int,
    char_cap: int,
) -> list[ContextHit]:
    """Add 1-hop relationship edges for the top-K code hits.

    Graph hits don't carry the partner's source body — they're cheap pointers
    so the agent knows what to look up next. Their score is the parent hit's
    score minus a small penalty so they sort just below their parent.
    """
    code_hits = [h for h in hits if h.component_id is not None][:top_k]
    if not code_hits:
        return []

    out: list[ContextHit] = []
    seen: set[tuple[str, str, str]] = set()  # (parent_id, partner_id, rel_type)

    with MetadataIndex(db_path) as index:
        for parent in code_hits:
            edges = index.relationships_of(parent.component_id, direction="both")
            for edge in edges:
                key = (
                    parent.component_id or "",
                    edge.partner.id,
                    edge.relationship_type,
                )
                if key in seen:
                    continue
                seen.add(key)
                arrow = "->" if edge.direction == "outgoing" else "<-"
                out.append(ContextHit(
                    source="graph",
                    kind=edge.relationship_type,
                    title=f"{parent.title} {arrow} {edge.partner.api_name}",
                    body=_truncate(
                        f"{edge.relationship_type}: "
                        f"{parent.component_id} {arrow} {edge.partner.id}",
                        char_cap,
                    ),
                    # Slight penalty so graph rows sort just below their parent.
                    score=max(0.0, parent.score - 0.05),
                    component_id=edge.partner.id,
                    citation=f"graph:{edge.relationship_type}:{edge.partner.id}",
                    metadata={
                        "parent_id": parent.component_id,
                        "direction": edge.direction,
                        "partner_type": edge.partner.component_type,
                        "edge_metadata": edge.metadata,
                    },
                ))
    return out


# ---------------------------------------------------------------------------
# Dedupe + budget
# ---------------------------------------------------------------------------

def _dedupe_by_component(hits: list[ContextHit]) -> list[ContextHit]:
    """Collapse semantic + literal hits of the same component into one row.

    Keep the higher-scoring source as primary; record the loser in metadata
    under `also_surfaced_by` so the agent can see the cross-layer agreement.
    Knowledge hits and graph edges are passed through unchanged (they have
    no component_id collision).
    """
    by_component: dict[str, ContextHit] = {}
    pass_through: list[ContextHit] = []

    for hit in hits:
        if hit.component_id is None or hit.source == "graph":
            pass_through.append(hit)
            continue
        existing = by_component.get(hit.component_id)
        if existing is None:
            by_component[hit.component_id] = hit
            continue
        # Same component already in the working set — merge.
        if hit.score > existing.score:
            also = list(existing.metadata.get("also_surfaced_by", []))
            also.append({"source": existing.source, "score": existing.score})
            hit.metadata = {**hit.metadata, "also_surfaced_by": also}
            by_component[hit.component_id] = hit
        else:
            also = list(existing.metadata.get("also_surfaced_by", []))
            also.append({"source": hit.source, "score": hit.score})
            existing.metadata = {**existing.metadata, "also_surfaced_by": also}

    return list(by_component.values()) + pass_through


def _fit_to_budget(
    hits: list[ContextHit], max_tokens: int
) -> tuple[list[ContextHit], int, int]:
    """Greedy fit by score-desc order. Returns (kept, dropped_count, total_tokens)."""
    kept: list[ContextHit] = []
    used = 0
    dropped = 0
    for hit in hits:
        cost = hit.estimated_tokens
        if used + cost > max_tokens and kept:  # always keep at least one
            dropped += 1
            continue
        kept.append(hit)
        used += cost
    return kept, dropped, used


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _component_to_hit(
    *, comp: ComponentRow, source: str, score: float, char_cap: int
) -> ContextHit:
    body = _truncate(comp.source or "", char_cap) if comp.source else ""
    return ContextHit(
        source=source,
        kind=comp.component_type,
        title=comp.api_name,
        body=body,
        score=score,
        component_id=comp.id,
        citation=comp.id,
        metadata={
            "parent_id": comp.parent_id,
            "file_path": comp.file_path,
            "last_indexed_at": comp.last_indexed_at,
            "component_metadata": comp.metadata,
        },
    )


def _truncate(text: str, char_cap: int) -> str:
    if len(text) <= char_cap:
        return text
    # Cut at the last newline before the cap when possible — preserves
    # whole-line readability for source code.
    cut = text.rfind("\n", 0, char_cap)
    cut = cut if cut > char_cap // 2 else char_cap
    return text[:cut].rstrip() + "\n…[truncated]"


def _clip01(score: float) -> float:
    if score < 0.0:
        return 0.0
    if score > 1.0:
        return 1.0
    return score


def _default_db_path() -> Path:
    # Local import so this module stays import-cheap when Gemini deps aren't
    # installed (the deferred import in `_make_query_embedder` mirrors this).
    from sf_dev_agent.context import default_db_path
    return default_db_path()


def _make_query_embedder() -> Embedder:
    """Resolve a query-side embedder; tries RETRIEVAL_QUERY task type first."""
    try:
        return create_embedder(task_type="RETRIEVAL_QUERY")
    except (TypeError, ValueError):
        # Mock embedder doesn't accept task_type; fall back transparently.
        return create_embedder()


__all__ = [
    "ContextHit",
    "RetrievalResult",
    "retrieve_context",
]
