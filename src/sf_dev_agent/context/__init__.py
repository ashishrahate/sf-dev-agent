"""Hybrid context retrieval engine — vector store, metadata index, knowledge base.

Slice 1 (Week 3-6, in progress): the metadata index.

Public API:
    build_index(org_alias, db_path=None, retrieve_dir=None, cleanup_retrieve=True)
        Retrieve metadata from a live org and ingest it into the SQLite index.
        By default the staging directory is wiped after ingestion — the SQLite
        index is the durable source of truth, so the on-disk retrieve dir is
        purely a parser hand-off and shouldn't duplicate what's already stored.

    ingest_directory(source_dir, db_path=None, org_alias="local") -> IndexBuildResult
        Walk a local sfdx source tree and ingest it. Used by tests; also useful
        for indexing a checked-out repo without hitting the org.

    default_db_path() -> Path
        Where the index lives by default (.cache/metadata_index.db at repo root).

    MetadataIndex          # the SQLite-backed read/write class
    Parser, ParsedComponent, ParsedRelationship  # parser ABC + DTOs
    register              # decorator/function for registering custom parsers

Future scope — turn `build_index` into a refresh service:

    The current `build_index` is a one-shot. The natural evolutions are:

      - **Post-deploy hook**: invoke `build_index` (or a delta variant) after
        every successful `sf_source_deploy` so the index never lags behind the
        agent's own writes. Hook would scope the rebuild to the deployed
        component types only.

      - **Scheduled refresh**: a cron / scheduled agent / GitHub Action that
        rebuilds the index nightly to catch out-of-band changes (admins,
        other developers, packaged-app installs).

      - **Event-driven**: poll Tooling API SetupAuditTrail (or subscribe to a
        Streaming API channel) for metadata-mutation events; trigger a delta
        rebuild for the affected components only.

      - **Delta/incremental ingest**: compare the org's current ApexClass /
        CustomObject lastModifiedDate against `components.last_indexed_at` and
        only retrieve+parse the deltas. Required for any of the above to scale
        beyond toy orgs.

    The current code is structured for this: parsers are independent, the
    index is upsert-idempotent, and each ingestion run is recorded in
    `index_runs`. A scheduler would call `build_index(component_types=[...])`
    with a narrowed list and the index would converge.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from sf_dev_agent.context.delta import (
    SUPPORTED_DELTA_TYPES,
    DeltaPlan,
    OrgComponent,
    OrgInventory,
    compute_deltas,
    fetch_org_inventory,
)
from sf_dev_agent.context.embedders import (
    Embedder,
    MockEmbedder,
    create_embedder,
)
from sf_dev_agent.context.index import (
    ComponentRow,
    EmbeddingRefreshResult,
    MetadataIndex,
    RelationshipEdge,
    SemanticSearchHit,
)
from sf_dev_agent.context.knowledge import (
    KnowledgeBase,
    KnowledgeEntry,
    KnowledgeSearchHit,
    bundled_entries_dir,
)
from sf_dev_agent.context.knowledge.store import (
    KnowledgeEmbedResult,
    KnowledgeIngestResult,
)
from sf_dev_agent.context.orchestrator import (
    ContextHit,
    RetrievalResult,
    retrieve_context,
)
from sf_dev_agent.context.parsers import (
    ParsedComponent,
    ParsedRelationship,
    Parser,
    discovered_component_types,
    dispatch,
    get_parsers,
    register,
)
from sf_dev_agent.context.retriever import RetrieveResult, retrieve, retrieve_components
from sf_dev_agent.paths import repo_root

logger = logging.getLogger(__name__)


# Component types whose children (FK parent_id) may be added or removed
# between the org and the index. When delta-refresh re-fetches one of these,
# we wipe its existing children first so deleted-on-org rows don't linger.
_PARENT_TYPES: frozenset[str] = frozenset({"CustomObject"})


@dataclass
class IndexBuildResult:
    success: bool
    db_path: Path
    components_indexed: int = 0
    relationships_indexed: int = 0
    relationships_skipped: int = 0
    parser_errors: list[tuple[str, str]] = field(default_factory=list)
    retrieve_error: str | None = None
    component_types: list[str] = field(default_factory=list)
    # Delta-refresh fields. Set when build_index runs in delta mode.
    delta_mode: bool = False
    components_fetched: int = 0           # how many were retrieved from the org this run
    components_deleted: int = 0           # rows pruned because they no longer exist in the org
    components_unchanged: int = 0         # rows the delta planner skipped — still in the index
    inventory_errors: list[str] = field(default_factory=list)


def default_db_path() -> Path:
    return repo_root() / ".cache" / "metadata_index.db"


def default_component_types() -> list[str]:
    """Component types the live retriever asks the org for, by default.

    This is *separate* from `discovered_component_types()` — that's what's
    parseable; this is what we ask the CLI to retrieve. Some types (like
    StandardObject, ApexTriggerHistory) aren't retrievable but might be
    parseable, and vice versa.
    """
    return [
        "ApexClass", "ApexTrigger", "CustomObject",
        "ValidationRule", "RecordType", "Flow",
        "LightningComponentBundle",
    ]


# ---------------------------------------------------------------------------
# Local-directory ingestion (no org required)
# ---------------------------------------------------------------------------

def ingest_directory(
    source_dir: Path | str,
    db_path: Path | str | None = None,
    org_alias: str = "local",
) -> IndexBuildResult:
    """Walk a local sfdx source tree and ingest every file the parsers handle."""
    source_dir = Path(source_dir)
    db_path = Path(db_path) if db_path else default_db_path()

    if not source_dir.exists():
        return IndexBuildResult(
            success=False,
            db_path=db_path,
            retrieve_error=f"source_dir not found: {source_dir}",
        )

    components_indexed = 0
    relationships_indexed = 0
    relationships_skipped = 0
    parser_errors: list[tuple[str, str]] = []

    component_types = list(discovered_component_types())

    with MetadataIndex(db_path) as index:
        run_id = index.start_run(org_alias=org_alias, component_types=component_types)

        # Two-pass ingestion: components first, then relationships, so FK
        # constraints don't reject relationships pointing at not-yet-inserted rows.
        pending_relationships: list[ParsedRelationship] = []

        with index.transaction():
            for path in _walk_files(source_dir):
                parser = dispatch(path)
                if parser is None:
                    continue
                try:
                    result = parser.parse(path)
                except Exception as exc:  # parser bugs shouldn't kill the whole run
                    parser_errors.append((str(path), f"{type(exc).__name__}: {exc}"))
                    logger.warning("Parser failed for %s: %s", path, exc)
                    continue

                for component in result.components:
                    index.upsert_component(component)
                    components_indexed += 1
                pending_relationships.extend(result.relationships)

        with index.transaction():
            for rel in pending_relationships:
                if index.upsert_relationship(rel):
                    relationships_indexed += 1
                else:
                    relationships_skipped += 1

        index.finish_run(run_id, components_count=components_indexed)

    return IndexBuildResult(
        success=True,
        db_path=db_path,
        components_indexed=components_indexed,
        relationships_indexed=relationships_indexed,
        relationships_skipped=relationships_skipped,
        parser_errors=parser_errors,
        component_types=component_types,
    )


# ---------------------------------------------------------------------------
# Live-org ingestion
# ---------------------------------------------------------------------------

def build_index(
    org_alias: str,
    db_path: Path | str | None = None,
    retrieve_dir: Path | str | None = None,
    component_types: list[str] | None = None,
    cleanup_retrieve: bool = True,
    delta: bool = True,
) -> IndexBuildResult:
    """Retrieve metadata from a live org and ingest it into the SQLite index.

    With `delta=True` (default), only components whose Tooling-API
    `LastModifiedDate` is newer than the local `last_indexed_at` are
    retrieved, and components no longer present in the org are pruned. This
    keeps post-deploy refreshes cheap and bounded.

    Component types not currently supported by the delta planner (anything
    other than ApexClass, ApexTrigger today — see `SUPPORTED_DELTA_TYPES`)
    fall back to the full-retrieve path for those types only. The two paths
    co-exist in a single call: ApexClass + ApexTrigger refresh via delta,
    CustomObject (etc.) full-retrieves, both ingest into the same DB.

    Pass `delta=False` to force a full retrieve of every type (useful as a
    "rebuild from scratch" escape hatch — e.g. after a schema migration).

    The retrieve staging directory is wiped after a successful ingestion so
    the on-disk source doesn't duplicate the SQLite copy. On failure the
    directory is preserved for debugging.
    """
    db_path = Path(db_path) if db_path else default_db_path()
    retrieve_dir = (
        Path(retrieve_dir) if retrieve_dir
        else repo_root() / ".cache" / "retrieve" / org_alias
    )
    types = component_types or default_component_types()

    if delta:
        return _build_index_delta(
            org_alias=org_alias,
            db_path=db_path,
            retrieve_dir=retrieve_dir,
            component_types=types,
            cleanup_retrieve=cleanup_retrieve,
        )
    return _build_index_full(
        org_alias=org_alias,
        db_path=db_path,
        retrieve_dir=retrieve_dir,
        component_types=types,
        cleanup_retrieve=cleanup_retrieve,
    )


def _build_index_full(
    *,
    org_alias: str,
    db_path: Path,
    retrieve_dir: Path,
    component_types: list[str],
    cleanup_retrieve: bool,
) -> IndexBuildResult:
    """Full-refresh path — retrieve everything for the requested types."""
    retrieve_result = retrieve(
        org_alias=org_alias,
        component_types=component_types,
        target_dir=retrieve_dir,
    )

    if not retrieve_result.success:
        return IndexBuildResult(
            success=False,
            db_path=db_path,
            component_types=component_types,
            retrieve_error=retrieve_result.error,
        )

    ingest_result = ingest_directory(
        source_dir=retrieve_result.output_dir,
        db_path=db_path,
        org_alias=org_alias,
    )
    ingest_result.components_fetched = ingest_result.components_indexed
    ingest_result.delta_mode = False

    if ingest_result.success and cleanup_retrieve:
        _try_cleanup(retrieve_dir)

    return ingest_result


def _build_index_delta(
    *,
    org_alias: str,
    db_path: Path,
    retrieve_dir: Path,
    component_types: list[str],
    cleanup_retrieve: bool,
) -> IndexBuildResult:
    """Delta-refresh path — only fetch what changed.

    Strategy:
      - Split requested types into delta-supported (ApexClass, ApexTrigger)
        and unsupported (everything else).
      - For unsupported types, run the existing full-retrieve.
      - For supported types, fetch the org's Tooling-API inventory, diff it
        against `MetadataIndex.inventory_for_types(...)`, retrieve only the
        deltas, and prune deletions from the index.
      - Both paths land their source under the same retrieve dir and ingest
        in one pass so relationship resolution sees every component together.
    """
    delta_types = [t for t in component_types if t in SUPPORTED_DELTA_TYPES]
    full_types = [t for t in component_types if t not in SUPPORTED_DELTA_TYPES]

    inventory_errors: list[str] = []
    plan: DeltaPlan | None = None

    # --- Phase A: full-retrieve unsupported types (CustomObject, etc.) ---
    full_retrieve_result: RetrieveResult | None = None
    if full_types:
        full_retrieve_result = retrieve(
            org_alias=org_alias,
            component_types=full_types,
            target_dir=retrieve_dir,
        )
        if not full_retrieve_result.success:
            return IndexBuildResult(
                success=False,
                db_path=db_path,
                component_types=component_types,
                retrieve_error=full_retrieve_result.error,
                delta_mode=True,
                inventory_errors=inventory_errors,
            )

    # --- Phase B: compute deltas for supported types ---
    components_deleted = 0
    components_unchanged = 0
    delta_retrieve_result: RetrieveResult | None = None

    if delta_types:
        inventory = fetch_org_inventory(org_alias=org_alias, component_types=delta_types)
        inventory_errors.extend(inventory.errors)

        with MetadataIndex(db_path) as index:
            indexed = index.inventory_for_types(delta_types)

        plan = compute_deltas(
            inventory=inventory,
            indexed=indexed,
            requested_types=delta_types,
        )
        components_unchanged = len(plan.unchanged)
        logger.info(
            "Delta plan: fetch=%d delete=%d unchanged=%d",
            len(plan.to_fetch), len(plan.to_delete), len(plan.unchanged),
        )

        # Prune deletions before ingestion so a re-created component isn't
        # accidentally orphaned.
        if plan.to_delete:
            with MetadataIndex(db_path) as index:
                components_deleted = index.delete_components(plan.to_delete)

        # Wipe stale children of parents we're about to re-fetch. A
        # CustomObject that lost a field would otherwise leave that field's
        # row behind as an orphan — the upsert can't see it disappear, only
        # an explicit delete here can.
        parents_being_refreshed = [
            cid for cid in plan.to_fetch
            if cid.split(":", 1)[0] in _PARENT_TYPES
        ]
        if parents_being_refreshed:
            with MetadataIndex(db_path) as index:
                index.delete_children_of(parents_being_refreshed)

        # Targeted retrieve.
        if plan.to_fetch:
            delta_retrieve_result = retrieve_components(
                org_alias=org_alias,
                component_ids=plan.to_fetch,
                target_dir=retrieve_dir,
            )
            if not delta_retrieve_result.success:
                return IndexBuildResult(
                    success=False,
                    db_path=db_path,
                    component_types=component_types,
                    retrieve_error=delta_retrieve_result.error,
                    delta_mode=True,
                    components_deleted=components_deleted,
                    components_unchanged=components_unchanged,
                    inventory_errors=inventory_errors,
                )

    # --- Phase C: ingest whatever landed in retrieve_dir ---
    # The dir holds source from BOTH the unsupported-types full retrieve and
    # the supported-types targeted retrieve; ingest_directory walks everything
    # in one pass. If neither phase produced files (delta with no changes,
    # nothing else requested), short-circuit.
    nothing_landed = (
        full_retrieve_result is None
        and (delta_retrieve_result is None or not (plan and plan.to_fetch))
    )

    if nothing_landed:
        result = IndexBuildResult(
            success=True,
            db_path=db_path,
            component_types=component_types,
            delta_mode=True,
            components_fetched=0,
            components_deleted=components_deleted,
            components_unchanged=components_unchanged,
            inventory_errors=inventory_errors,
        )
        # Even with no fetched files, the retrieve dir may have been created
        # earlier — clean up if asked.
        if cleanup_retrieve:
            _try_cleanup(retrieve_dir)
        return result

    ingest_result = ingest_directory(
        source_dir=retrieve_dir,
        db_path=db_path,
        org_alias=org_alias,
    )
    ingest_result.delta_mode = True
    ingest_result.components_fetched = ingest_result.components_indexed
    ingest_result.components_deleted = components_deleted
    ingest_result.components_unchanged = components_unchanged
    ingest_result.inventory_errors = inventory_errors

    if ingest_result.success and cleanup_retrieve:
        _try_cleanup(retrieve_dir)

    return ingest_result


def _try_cleanup(path: Path) -> None:
    """Wipe the retrieve staging dir; non-fatal on failure."""
    try:
        shutil.rmtree(path)
        logger.info("Cleaned up retrieve staging dir: %s", path)
    except OSError as exc:
        logger.warning("Failed to clean up %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _walk_files(root: Path):
    """Yield every file under `root` (skips directories the SF CLI manages)."""
    skip_dirs = {".sfdx", ".sf", "node_modules", "__pycache__"}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in skip_dirs for part in path.parts):
            continue
        yield path


def embed_index(
    db_path: Path | str | None = None,
    embedder: Embedder | None = None,
    component_types: list[str] | None = None,
    force: bool = False,
) -> EmbeddingRefreshResult:
    """Populate / refresh embeddings for components in the index.

    With no `embedder`, resolves one from env (Gemini if `GOOGLE_API_KEY` set,
    else MockEmbedder). Hash-gated by default — pass `force=True` to re-embed
    every row (e.g. after switching models).
    """
    db_path = Path(db_path) if db_path else default_db_path()
    if not db_path.exists():
        return EmbeddingRefreshResult(
            errors=[f"Index not found at {db_path}; run build_index first"],
        )
    if embedder is None:
        embedder = create_embedder()

    with MetadataIndex(db_path) as index:
        return index.embed_components(
            embedder=embedder,
            component_types=component_types,
            force=force,
        )


def embed_knowledge(
    db_path: Path | str | None = None,
    embedder: Embedder | None = None,
    category: str | None = None,
    force: bool = False,
) -> KnowledgeEmbedResult:
    """Auto-load bundled entries (if needed) and refresh embeddings.

    Mirrors `embed_index` for the metadata side: hash-gated by default;
    pass `force=True` to re-embed every entry (e.g. after switching models).
    """
    db_path = Path(db_path) if db_path else default_db_path()
    if embedder is None:
        embedder = create_embedder()
    with KnowledgeBase(db_path) as kb:
        kb.auto_load_if_empty()
        return kb.embed_entries(embedder=embedder, category=category, force=force)


__all__ = [
    "IndexBuildResult",
    "MetadataIndex",
    "ComponentRow",
    "RelationshipEdge",
    "SemanticSearchHit",
    "EmbeddingRefreshResult",
    "Embedder",
    "MockEmbedder",
    "create_embedder",
    "embed_index",
    "embed_knowledge",
    "KnowledgeBase",
    "KnowledgeEntry",
    "KnowledgeSearchHit",
    "bundled_entries_dir",
    "DeltaPlan",
    "OrgComponent",
    "OrgInventory",
    "SUPPORTED_DELTA_TYPES",
    "compute_deltas",
    "fetch_org_inventory",
    "ContextHit",
    "RetrievalResult",
    "retrieve_context",
    "ParsedComponent",
    "ParsedRelationship",
    "Parser",
    "RetrieveResult",
    "build_index",
    "default_component_types",
    "default_db_path",
    "discovered_component_types",
    "get_parsers",
    "ingest_directory",
    "register",
]
