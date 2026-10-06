"""Transport-agnostic service layer for the context engine.

One implementation of every context-engine operation (index queries,
dependency graph, knowledge search, project memory, retrieval). Both the
sf-dev-agent `ToolRegistry` and the `sf-context-mcp` MCP server call into
this class, so behaviour is identical regardless of the front end.

Every public method returns a JSON-serialisable dict; failures are returned
as ``{"error": ...}`` rather than raised.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sf_context_engine.memory import MemoryScope


class ContextService:
    """Context-engine operations bound to one (db, org, tenant)."""

    # Default head-N lines for a returned `source` blob in code_search /
    # retrieve_context. Pass source_max_lines=0 to disable trimming.
    _DEFAULT_SOURCE_MAX_LINES = 80

    def __init__(
        self,
        org_alias: str,
        tenant_id: str = "local-dev",
        db_path: Path | None = None,
    ) -> None:
        self.org_alias = org_alias
        self.tenant_id = tenant_id
        self.db_path = db_path  # None -> default location resolved lazily

    def _resolve_index_db_path(self) -> Path:
        """Return the configured DB path, or the package default."""
        if self.db_path:
            return self.db_path
        from sf_context_engine import default_db_path
        return default_db_path()

    def _index_missing_response(self, db_path: Path) -> dict[str, Any]:
        return {
            "error": (
                f"Metadata index not found at {db_path}. "
                "Build it first by calling the build_metadata_index tool."
            ),
            "components": [],
            "components_indexed": 0,
        }

    @staticmethod
    def _component_summary(
        comp: Any,
        include_source: bool = False,
        source_max_lines: int | None = None,
    ) -> dict[str, Any]:
        """Pack a ComponentRow into a compact dict for tool output.

        When `source_max_lines` is positive and the component's source is
        longer than that, the returned `source` field is trimmed to the
        first N lines with a `… (N more lines, full body at <component_id>)`
        suffix. The agent can re-fetch the untrimmed body via
        `sf_metadata_describe` or by re-running with a higher cap.
        Pass `source_max_lines=0` to disable trimming.
        """
        out = {
            "id": comp.id,
            "component_type": comp.component_type,
            "api_name": comp.api_name,
            "parent_id": comp.parent_id,
            "metadata": comp.metadata,
            "last_indexed_at": comp.last_indexed_at,
        }
        if include_source and comp.source is not None:
            cap = (
                ContextService._DEFAULT_SOURCE_MAX_LINES
                if source_max_lines is None else source_max_lines
            )
            out["source"] = ContextService._maybe_trim_source(
                comp.source, comp.id, cap,
            )
        return out

    @staticmethod
    def _maybe_trim_source(source: str, component_id: str, cap: int) -> str:
        """Trim `source` to at most `cap` lines + a footer hint. cap<=0
        disables trimming entirely; cap>=lines is a no-op."""
        if cap <= 0:
            return source
        lines = source.splitlines()
        if len(lines) <= cap:
            return source
        remaining = len(lines) - cap
        head = "\n".join(lines[:cap])
        return (
            f"{head}\n… ({remaining} more line"
            f"{'s' if remaining != 1 else ''}, full body at {component_id})"
        )

    def _memory_scope(self, cross_org: bool = False) -> MemoryScope:
        """Build the MemoryScope for the current org.

        `cross_org=True` drops the org pin so a memory applies tenant-wide.
        Recall always uses the org-pinned scope (so it returns BOTH org-
        specific and cross-org rows under that tenant — see _scope_clause).
        """
        from sf_context_engine.memory import MemoryScope
        return MemoryScope(
            tenant_id=self.tenant_id,
            org_alias=None if cross_org else self.org_alias,
        )

    def code_search(
        self,
        query: str,
        component_type: str | None = None,
        include_source: bool = False,
        limit: int = 25,
        source_max_lines: int | None = None,
    ) -> dict[str, Any]:
        """Substring search across the metadata index.

        `source_max_lines` only applies when `include_source=True`. Default
        is class-level: trim to ~80 lines per hit. Pass 0 to get untrimmed
        bodies (useful when the agent specifically needs full code).
        """
        from sf_context_engine import MetadataIndex

        db_path = self._resolve_index_db_path()
        if not db_path.exists():
            return self._index_missing_response(db_path)

        limit = max(1, min(limit, 100))
        with MetadataIndex(db_path) as index:
            hits = index.search(query, component_type=component_type, limit=limit)
            return {
                "query": query,
                "component_type": component_type,
                "match_count": len(hits),
                "results": [
                    self._component_summary(
                        h, include_source, source_max_lines,
                    )
                    for h in hits
                ],
            }

    def dependency_graph(
        self,
        component_id: str | None = None,
        component_type: str | None = None,
        api_name: str | None = None,
        direction: str = "both",
    ) -> dict[str, Any]:
        """Return relationship edges for one component."""
        from sf_context_engine import MetadataIndex

        if direction not in ("outgoing", "incoming", "both"):
            return {"error": f"Invalid direction: {direction!r}"}

        db_path = self._resolve_index_db_path()
        if not db_path.exists():
            return self._index_missing_response(db_path)

        with MetadataIndex(db_path) as index:
            # Resolve the target component first.
            if component_id:
                target = index.find_by_id(component_id)
            elif component_type and api_name:
                matches = index.find_by_name(api_name, component_type=component_type)
                target = matches[0] if matches else None
            else:
                return {"error": "Provide either component_id or component_type+api_name"}

            if target is None:
                return {
                    "error": "Component not found in index",
                    "component_id": component_id,
                    "component_type": component_type,
                    "api_name": api_name,
                }

            edges = index.relationships_of(target.id, direction=direction)
            return {
                "component": self._component_summary(target),
                "direction": direction,
                "edge_count": len(edges),
                "edges": [
                    {
                        "direction": e.direction,
                        "relationship_type": e.relationship_type,
                        "partner": self._component_summary(e.partner),
                        "metadata": e.metadata,
                    }
                    for e in edges
                ],
            }

    def build_metadata_index(
        self,
        component_types: list[str] | None = None,
        full_refresh: bool = False,
    ) -> dict[str, Any]:
        """Refresh the SQLite metadata index from the connected org."""
        from sf_context_engine import build_index

        db_path = self._resolve_index_db_path()
        result = build_index(
            org_alias=self.org_alias,
            db_path=db_path,
            component_types=component_types,
            delta=not full_refresh,
        )
        return {
            "success": result.success,
            "db_path": str(result.db_path),
            "delta_mode": result.delta_mode,
            "components_indexed": result.components_indexed,
            "components_fetched": result.components_fetched,
            "components_deleted": result.components_deleted,
            "components_unchanged": result.components_unchanged,
            "relationships_indexed": result.relationships_indexed,
            "relationships_skipped": result.relationships_skipped,
            "parser_errors": result.parser_errors,
            "retrieve_error": result.retrieve_error,
            "inventory_errors": result.inventory_errors,
            "component_types": result.component_types,
        }

    def embed_metadata_index(
        self,
        component_types: list[str] | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Populate/refresh embeddings for indexed components."""
        from sf_context_engine import MetadataIndex, create_embedder

        db_path = self._resolve_index_db_path()
        if not db_path.exists():
            return self._index_missing_response(db_path)

        try:
            embedder = create_embedder()
        except (ValueError, ImportError) as exc:
            return {"error": f"Could not initialize embedder: {exc}"}

        with MetadataIndex(db_path) as index:
            try:
                result = index.embed_components(
                    embedder=embedder,
                    component_types=component_types,
                    force=force,
                )
            except Exception as exc:
                return {"error": f"Embedding failed: {type(exc).__name__}: {exc}"}
            stats = index.embedding_stats()

        return {
            "embedder": result.embedder_name,
            "embedded": result.embedded,
            "skipped_unchanged": result.skipped_unchanged,
            "skipped_no_source": result.skipped_no_source,
            "errors": result.errors,
            "coverage": stats,
        }

    def embed_knowledge_base(
        self,
        category: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Auto-load bundled entries (if needed) and refresh embeddings."""
        from sf_context_engine import KnowledgeBase, create_embedder

        try:
            embedder = create_embedder()
        except (ValueError, ImportError) as exc:
            return {"error": f"Could not initialize embedder: {exc}"}

        db_path = self._resolve_index_db_path()
        with KnowledgeBase(db_path) as kb:
            ingest = kb.auto_load_if_empty()
            try:
                embed_result = kb.embed_entries(
                    embedder=embedder, category=category, force=force,
                )
            except Exception as exc:
                return {"error": f"Embedding failed: {type(exc).__name__}: {exc}"}
            stats = kb.embedding_stats()

        return {
            "embedder": embed_result.embedder_name,
            "entries_loaded": ingest.loaded,
            "entries_updated": ingest.updated,
            "entries_skipped_unchanged": ingest.skipped_unchanged,
            "embedded": embed_result.embedded,
            "skipped_unchanged": embed_result.skipped_unchanged,
            "errors": embed_result.errors + [
                f"{path}: {err}" for path, err in ingest.parse_errors
            ],
            "coverage": stats,
        }

    def knowledge_search(
        self,
        query: str,
        category: str | None = None,
        limit: int = 5,
        min_score: float = 0.0,
    ) -> dict[str, Any]:
        """Embed the query and rank knowledge entries by cosine similarity."""
        from sf_context_engine import KnowledgeBase, create_embedder

        limit = max(1, min(limit, 25))

        try:
            try:
                embedder = create_embedder(task_type="RETRIEVAL_QUERY")
            except (TypeError, ValueError):
                embedder = create_embedder()
        except (ValueError, ImportError) as exc:
            return {"error": f"Could not initialize embedder: {exc}"}

        try:
            query_vec = embedder.embed_one(query)
        except Exception as exc:
            return {"error": f"Embedding the query failed: {type(exc).__name__}: {exc}"}

        db_path = self._resolve_index_db_path()
        with KnowledgeBase(db_path) as kb:
            kb.auto_load_if_empty()
            hits = kb.search(
                query_embedding=query_vec, category=category, limit=limit,
            )

        if not hits:
            return {
                "query": query,
                "category": category,
                "embedder": embedder.name,
                "match_count": 0,
                "results": [],
                "note": (
                    "No embedded knowledge entries to search. "
                    "Run embed_knowledge_base first."
                ),
            }

        filtered = [h for h in hits if h.score >= min_score]
        if not filtered:
            return {
                "query": query,
                "category": category,
                "embedder": embedder.name,
                "match_count": 0,
                "best_score_below_threshold": hits[0].score,
                "min_score": min_score,
                "results": [],
            }

        return {
            "query": query,
            "category": category,
            "embedder": embedder.name,
            "match_count": len(filtered),
            "results": [
                {
                    "id": h.entry.id,
                    "title": h.entry.title,
                    "category": h.entry.category,
                    "severity": h.entry.severity,
                    "tags": h.entry.tags,
                    "references": h.entry.references,
                    "body": h.entry.body,
                    "score": round(h.score, 4),
                }
                for h in filtered
            ],
        }

    def memory_save(
        self,
        type: str,
        name: str,
        description: str,
        body: str,
        tags: list[str] | None = None,
        cross_org: bool = False,
    ) -> dict[str, Any]:
        """Persist a memory row, scoped to the current (tenant, org).

        Embedding happens lazily — `memory_recall` does not auto-embed; the
        first explicit recall after a save will only see this row if
        `embed_memories` (or the orchestrator's batch embed) has run. This
        mirrors the metadata-index / knowledge-base separation between
        ingestion and embedding.
        """
        from sf_context_engine.memory import MemoryStore

        db_path = self._resolve_index_db_path()
        scope = self._memory_scope(cross_org=cross_org)

        try:
            with MemoryStore(db_path) as store:
                record = store.save(
                    scope=scope,
                    type=type,
                    name=name,
                    description=description,
                    body=body,
                    tags=tags or [],
                )
        except ValueError as exc:
            return {"error": str(exc)}

        return {
            "saved": True,
            "id": record.id,
            "type": record.type,
            "name": record.name,
            "tenant_id": record.tenant_id,
            "org_alias": record.org_alias,
            "created_at": record.created_at,
            "note": (
                "Embedding is lazy — recall will not return this row until "
                "embeddings are refreshed. Run embed_memories or rely on the "
                "orchestrator's batch embed."
            ),
        }

    def memory_recall(
        self,
        query: str,
        type: str | None = None,
        limit: int = 5,
        min_score: float = 0.0,
    ) -> dict[str, Any]:
        """Embed the query and rank memories in scope by cosine similarity."""
        from sf_context_engine import create_embedder
        from sf_context_engine.memory import MemoryStore

        limit = max(1, min(limit, 25))

        try:
            try:
                embedder = create_embedder(task_type="RETRIEVAL_QUERY")
            except (TypeError, ValueError):
                embedder = create_embedder()
        except (ValueError, ImportError) as exc:
            return {"error": f"Could not initialize embedder: {exc}"}

        try:
            query_vec = embedder.embed_one(query)
        except Exception as exc:
            return {"error": f"Embedding the query failed: {type(exc).__name__}: {exc}"}

        db_path = self._resolve_index_db_path()
        scope = self._memory_scope()

        try:
            with MemoryStore(db_path) as store:
                hits = store.recall(
                    query_embedding=query_vec,
                    scope=scope,
                    type=type,
                    limit=limit,
                )
        except ValueError as exc:
            return {"error": str(exc)}

        if not hits:
            return {
                "query": query,
                "type": type,
                "embedder": embedder.name,
                "match_count": 0,
                "results": [],
                "note": (
                    "No embedded memories in scope. Either nothing has been "
                    "saved yet, or embeddings haven't been refreshed since "
                    "the last save."
                ),
            }

        filtered = [h for h in hits if h.score >= min_score]
        if not filtered:
            return {
                "query": query,
                "type": type,
                "embedder": embedder.name,
                "match_count": 0,
                "best_score_below_threshold": hits[0].score,
                "min_score": min_score,
                "results": [],
            }

        return {
            "query": query,
            "type": type,
            "embedder": embedder.name,
            "match_count": len(filtered),
            "results": [
                {
                    "id": h.record.id,
                    "type": h.record.type,
                    "name": h.record.name,
                    "description": h.record.description,
                    "body": h.record.body,
                    "tags": h.record.tags,
                    "tenant_id": h.record.tenant_id,
                    "org_alias": h.record.org_alias,
                    "created_at": h.record.created_at,
                    "score": round(h.score, 4),
                }
                for h in filtered
            ],
        }

    def memory_list(
        self,
        type: str | None = None,
        limit: int = 25,
        include_superseded: bool = False,
    ) -> dict[str, Any]:
        """List memories in scope without embedding cost."""
        from sf_context_engine.memory import MemoryStore

        limit = max(1, min(limit, 100))
        db_path = self._resolve_index_db_path()
        scope = self._memory_scope()

        try:
            with MemoryStore(db_path) as store:
                records = store.list(
                    scope=scope,
                    type=type,
                    include_superseded=include_superseded,
                    limit=limit,
                )
        except ValueError as exc:
            return {"error": str(exc)}

        return {
            "tenant_id": scope.tenant_id,
            "org_alias": scope.org_alias,
            "type": type,
            "count": len(records),
            "memories": [
                {
                    "id": r.id,
                    "type": r.type,
                    "name": r.name,
                    "description": r.description,
                    "tags": r.tags,
                    "org_alias": r.org_alias,
                    "created_at": r.created_at,
                    "last_accessed_at": r.last_accessed_at,
                    "access_count": r.access_count,
                    "superseded_by": r.superseded_by,
                }
                for r in records
            ],
        }

    def check_index_freshness(self) -> dict[str, Any]:
        """Re-probe `index_runs` + components.embedding for the current org."""
        from sf_context_engine.index_freshness import (
            check_freshness,
            format_freshness_line,
        )

        db_path = self._resolve_index_db_path()
        freshness = check_freshness(db_path, self.org_alias)
        return {
            "org_alias": freshness.org_alias,
            "last_built_at": freshness.last_built_at,
            "age_seconds": freshness.age_seconds,
            "is_stale": freshness.is_stale,
            "embedding_coverage_pct": round(freshness.embedding_coverage_pct, 2),
            "components_count": freshness.components_count,
            "embedded_count": freshness.embedded_count,
            "last_run_error": freshness.last_run_error,
            "freshness_line": format_freshness_line(freshness),
        }

    def memory_compact(
        self,
        type: str | None = None,
        threshold: float = 0.85,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Surface clusters of similar memories for the agent to merge."""
        from sf_context_engine.memory import MemoryStore

        limit = max(1, min(limit, 25))
        db_path = self._resolve_index_db_path()
        scope = self._memory_scope()

        try:
            with MemoryStore(db_path) as store:
                clusters = store.find_merge_candidates(
                    scope=scope,
                    type=type,
                    threshold=threshold,
                    max_clusters=limit,
                )
        except ValueError as exc:
            return {"error": str(exc)}

        return {
            "tenant_id": scope.tenant_id,
            "org_alias": scope.org_alias,
            "threshold": threshold,
            "cluster_count": len(clusters),
            "clusters": [
                {
                    "type": c.type,
                    "average_similarity": round(c.average_similarity, 4),
                    "size": len(c.members),
                    "members": [
                        {
                            "id": m.id,
                            "name": m.name,
                            "description": m.description,
                            "body": m.body,
                            "tags": m.tags,
                            "created_at": m.created_at,
                            "last_accessed_at": m.last_accessed_at,
                            "access_count": m.access_count,
                        }
                        for m in c.members
                    ],
                }
                for c in clusters
            ],
            "note": (
                "To act on a cluster: write a single consolidated memory "
                "with memory_save, then call memory_supersede(old_id, "
                "new_id) for each member you're folding in."
            ),
        }

    def memory_supersede(
        self,
        old_id: str,
        new_id: str,
    ) -> dict[str, Any]:
        """Link old_id -> new_id; old becomes invisible to recall + list."""
        from sf_context_engine.memory import MemoryStore

        if old_id == new_id:
            return {"error": "old_id and new_id must differ"}

        db_path = self._resolve_index_db_path()
        with MemoryStore(db_path) as store:
            old = store.find_by_id(old_id)
            new = store.find_by_id(new_id)
            if old is None:
                return {"error": f"old memory {old_id!r} not found"}
            if new is None:
                return {"error": f"new memory {new_id!r} not found"}
            if old.superseded_by is not None:
                return {
                    "error": (
                        f"memory {old_id!r} is already superseded by "
                        f"{old.superseded_by!r}; refusing to chain"
                    ),
                }
            store.supersede(old_id=old_id, new_id=new_id)

        return {
            "superseded": True,
            "old_id": old_id,
            "new_id": new_id,
            "note": (
                "The old memory is preserved on disk for auditability "
                "(use memory_list with include_superseded=true to see it). "
                "It will no longer surface in recall."
            ),
        }

    def retrieve_context(
        self,
        query: str,
        max_tokens: int = 4000,
        max_per_layer: int = 6,
        enrich_top_k: int = 3,
        component_type: str | None = None,
        knowledge_category: str | None = None,
        memory_type: str | None = None,
    ) -> dict[str, Any]:
        """Fan out to all four context layers, dedupe, graph-enrich, budget-trim."""
        from sf_context_engine import retrieve_context

        db_path = self._resolve_index_db_path()
        if not db_path.exists():
            return self._index_missing_response(db_path)

        result = retrieve_context(
            query=query,
            db_path=db_path,
            max_tokens=max_tokens,
            max_per_layer=max_per_layer,
            enrich_top_k=enrich_top_k,
            component_type=component_type,
            knowledge_category=knowledge_category,
            memory_scope=self._memory_scope(),
            memory_type=memory_type,
        )
        return result.to_dict()

    def semantic_search(
        self,
        query: str,
        component_type: str | None = None,
        limit: int = 10,
        min_score: float = 0.0,
    ) -> dict[str, Any]:
        """Embed the query and rank components by cosine similarity."""
        from sf_context_engine import MetadataIndex, create_embedder

        db_path = self._resolve_index_db_path()
        if not db_path.exists():
            return self._index_missing_response(db_path)

        limit = max(1, min(limit, 50))

        try:
            # For Gemini, queries should use task_type=RETRIEVAL_QUERY (different
            # optimization than the document side). The factory accepts kwargs
            # but the default path uses RETRIEVAL_DOCUMENT — try to override
            # if we can; otherwise fall back gracefully.
            try:
                embedder = create_embedder(task_type="RETRIEVAL_QUERY")
            except (TypeError, ValueError):
                # Fallback when the embedder doesn't support task_type
                # (e.g. the mock embedder).
                embedder = create_embedder()
        except (ValueError, ImportError) as exc:
            return {"error": f"Could not initialize embedder: {exc}"}

        try:
            query_vec = embedder.embed_one(query)
        except Exception as exc:
            return {"error": f"Embedding the query failed: {type(exc).__name__}: {exc}"}

        with MetadataIndex(db_path) as index:
            hits = index.semantic_search(
                query_embedding=query_vec,
                component_type=component_type,
                limit=limit,
            )

        filtered = [h for h in hits if h.score >= min_score]
        if not filtered and hits:
            # Surface the highest-scoring hit anyway so the agent knows the
            # best match (even if it's below threshold). Useful diagnostic.
            best_hit_score = hits[0].score
            return {
                "query": query,
                "component_type": component_type,
                "embedder": embedder.name,
                "match_count": 0,
                "best_score_below_threshold": best_hit_score,
                "min_score": min_score,
                "results": [],
            }

        return {
            "query": query,
            "component_type": component_type,
            "embedder": embedder.name,
            "match_count": len(filtered),
            "results": [
                {
                    **self._component_summary(h.component, include_source=False),
                    "score": round(h.score, 4),
                }
                for h in filtered
            ],
        }

    def index_status(self) -> dict[str, Any]:
        """Index freshness plus which embedder is active (and whether it is a mock)."""
        from sf_context_engine import create_embedder

        db_path = self._resolve_index_db_path()
        if not db_path.exists():
            out: dict[str, Any] = {
                "org_alias": self.org_alias,
                "index_built": False,
                "hint": "No index yet. Call build_metadata_index to create it.",
            }
        else:
            out = {"index_built": True, **self.check_index_freshness()}
        try:
            embedder = create_embedder()
            out["embedder"] = embedder.name
            out["embedder_is_mock"] = embedder.name.startswith("mock")
            if out["embedder_is_mock"]:
                out["warning"] = (
                    "No GOOGLE_API_KEY set: semantic_search, knowledge_search and "
                    "memory_recall use a hash-based mock embedder and are NOT "
                    "semantically meaningful. Set GOOGLE_API_KEY for real embeddings."
                )
        except (ValueError, ImportError) as exc:
            out["embedder"] = None
            out["embedder_error"] = str(exc)
        return out
