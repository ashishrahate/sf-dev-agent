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

from sf_dev_agent.context.index import ComponentRow, MetadataIndex
from sf_dev_agent.context.parsers import (
    ParsedComponent,
    ParsedRelationship,
    Parser,
    discovered_component_types,
    dispatch,
    get_parsers,
    register,
)
from sf_dev_agent.context.retriever import RetrieveResult, retrieve
from sf_dev_agent.paths import repo_root

logger = logging.getLogger(__name__)


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


def default_db_path() -> Path:
    return repo_root() / ".cache" / "metadata_index.db"


def default_component_types() -> list[str]:
    """Component types the live retriever asks the org for, by default.

    This is *separate* from `discovered_component_types()` — that's what's
    parseable; this is what we ask the CLI to retrieve. Some types (like
    StandardObject, ApexTriggerHistory) aren't retrievable but might be
    parseable, and vice versa.
    """
    return ["ApexClass", "ApexTrigger", "CustomObject"]


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
) -> IndexBuildResult:
    """Retrieve metadata from a live org and ingest it into the SQLite index.

    The retrieve staging directory is wiped after a successful ingestion so the
    on-disk source doesn't duplicate what's already in the SQLite `source`
    column. Pass `cleanup_retrieve=False` to keep the directory for
    inspection/debugging. On failure the directory is preserved regardless,
    so you can see what the CLI returned.
    """
    db_path = Path(db_path) if db_path else default_db_path()
    retrieve_dir = (
        Path(retrieve_dir) if retrieve_dir
        else repo_root() / ".cache" / "retrieve" / org_alias
    )
    types = component_types or default_component_types()

    retrieve_result = retrieve(
        org_alias=org_alias,
        component_types=types,
        target_dir=retrieve_dir,
    )

    if not retrieve_result.success:
        return IndexBuildResult(
            success=False,
            db_path=db_path,
            component_types=types,
            retrieve_error=retrieve_result.error,
        )

    ingest_result = ingest_directory(
        source_dir=retrieve_result.output_dir,
        db_path=db_path,
        org_alias=org_alias,
    )

    if ingest_result.success and cleanup_retrieve:
        try:
            shutil.rmtree(retrieve_dir)
            logger.info("Cleaned up retrieve staging dir: %s", retrieve_dir)
        except OSError as exc:
            # Cleanup failure shouldn't fail the whole build — the index is fine,
            # the user just has a stale dir they can manually delete.
            logger.warning("Failed to clean up %s: %s", retrieve_dir, exc)

    return ingest_result


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


__all__ = [
    "IndexBuildResult",
    "MetadataIndex",
    "ComponentRow",
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
