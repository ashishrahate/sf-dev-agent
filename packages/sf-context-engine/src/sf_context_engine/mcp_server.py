"""MCP server exposing the context engine (`sf-context-mcp`).

Division of labour with Salesforce's own DX MCP server: DX MCP *acts* on the
org (deploy, retrieve, query, run tests); this server *understands* the org
(what exists, what depends on what, org-specific decisions, best practices).
No tool here writes to the org.

Usage:
    sf-context-mcp --org <alias> [--tenant ID] [--db PATH] [--transport stdio]

Env equivalents: SF_ORG_ALIAS, SF_CONTEXT_TENANT, SF_CONTEXT_DB, GOOGLE_API_KEY
(embeddings), SF_CONTEXT_HOME (state directory). A `.env` in the working
directory is loaded if python-dotenv is available.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

try:
    from mcp.server.fastmcp import Context, FastMCP
    from mcp.types import ToolAnnotations
except ImportError as exc:  # pragma: no cover - only without the extra
    raise ImportError(
        "The MCP server needs the 'mcp' extra. "
        "Run: pip install 'sf-context-engine[mcp]'"
    ) from exc

from sf_context_engine.service import ContextService

_READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
# Writes only to the local index / memory store, never to the Salesforce org.
_LOCAL_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True,
)
_MEMORY_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False,
)

_INSTRUCTIONS = (
    "Context engine for one Salesforce org: an index of its Apex, objects, flows, "
    "validation rules, record types and LWCs with a dependency graph, a curated "
    "Salesforce best-practices knowledge base, and durable project memory. "
    "Start with retrieve_context for most questions. Use dependency_graph to see "
    "what depends on a component before changing it. This server never modifies "
    "the org; use the Salesforce DX MCP server (or sf CLI) to deploy, retrieve, "
    "query data or run tests. If index_status says the index is missing or stale, "
    "call build_metadata_index first."
)


def _sf_cli_path() -> str | None:
    return shutil.which("sf") or shutil.which("sf.cmd")


async def _run_with_heartbeat(
    ctx: Context, label: str, fn: Callable[..., dict[str, Any]], **kwargs: Any,
) -> dict[str, Any]:
    """Run a blocking call in a thread, pinging progress so clients don't time out."""
    task = asyncio.ensure_future(asyncio.to_thread(fn, **kwargs))
    elapsed = 0
    while True:
        done, _ = await asyncio.wait({task}, timeout=5)
        if done:
            return task.result()
        elapsed += 5
        try:
            await ctx.report_progress(elapsed, None, f"{label}... {elapsed}s")
        except Exception:  # progress is best-effort
            pass


def build_server(service: ContextService) -> FastMCP:
    mcp = FastMCP("sf-context", instructions=_INSTRUCTIONS)

    # ------------------------------------------------------------------ query

    @mcp.tool(annotations=_READ_ONLY)
    def retrieve_context(
        query: str,
        max_tokens: int = 2500,
        max_per_layer: int = 6,
        component_type: str | None = None,
        knowledge_category: str | None = None,
        memory_type: str | None = None,
    ) -> dict[str, Any]:
        """Default entry point. One call searches the org's indexed code, the
        best-practice knowledge base and saved project memory, merges and
        de-duplicates the hits, adds 1-hop dependency neighbours for the top
        results, and trims to max_tokens (default 2500; raise only if needed). Use it first for questions like "how is
        Account validated?" or "what handles order creation?". Optional filters:
        component_type (e.g. ApexClass), knowledge_category (anti_patterns,
        best_practices, governor_limits, patterns), memory_type (user, feedback,
        project, reference)."""
        return service.retrieve_context(
            query=query, max_tokens=max_tokens, max_per_layer=max_per_layer,
            component_type=component_type, knowledge_category=knowledge_category,
            memory_type=memory_type,
        )

    @mcp.tool(annotations=_READ_ONLY)
    def code_search(
        query: str,
        component_type: str | None = None,
        include_source: bool = False,
        limit: int = 25,
        source_max_lines: int | None = None,
    ) -> dict[str, Any]:
        """Exact substring search over the indexed metadata (names and source).
        Use for known identifiers (class, trigger, field or object names). Set
        include_source=true to get code (trimmed to ~80 lines per hit unless
        source_max_lines is set; 0 = untrimmed). With include_source, results are
        capped at 10 hits and 40 lines each unless you override source_max_lines."""
        if include_source:
            limit = min(limit, 10)
            if source_max_lines is None:
                source_max_lines = 40
        return service.code_search(
            query=query, component_type=component_type, include_source=include_source,
            limit=limit, source_max_lines=source_max_lines,
        )

    @mcp.tool(annotations=_READ_ONLY)
    def semantic_search(
        query: str,
        component_type: str | None = None,
        limit: int = 10,
        min_score: float = 0.0,
    ) -> dict[str, Any]:
        """Meaning-based search over embedded org code ("where do we validate
        revenue?"). Needs embeddings (build_metadata_index, then
        embed_metadata_index) and a real embedder: check
        index_status.embedder_is_mock."""
        return service.semantic_search(
            query=query, component_type=component_type, limit=limit, min_score=min_score,
        )

    @mcp.tool(annotations=_READ_ONLY)
    def dependency_graph(
        component_id: str | None = None,
        component_type: str | None = None,
        api_name: str | None = None,
        direction: str = "both",
    ) -> dict[str, Any]:
        """What a component uses (outgoing) and what uses it (incoming):
        class-to-class references, triggers on objects, flows, validation rules,
        record types, LWC imports. Check this before changing or deleting
        anything. Identify the target by component_id, or by component_type +
        api_name. direction: outgoing, incoming, both."""
        return service.dependency_graph(
            component_id=component_id, component_type=component_type,
            api_name=api_name, direction=direction,
        )

    @mcp.tool(annotations=_READ_ONLY)
    def knowledge_search(
        query: str,
        category: str | None = None,
        limit: int = 5,
        min_score: float = 0.0,
    ) -> dict[str, Any]:
        """Curated Salesforce guidance: governor limits, anti-patterns, trigger
        and bulkification patterns, testing and security practices. Platform-wide,
        not org-specific. category: anti_patterns, best_practices,
        governor_limits, patterns."""
        return service.knowledge_search(
            query=query, category=category, limit=limit, min_score=min_score,
        )

    # ------------------------------------------------------------------ index

    @mcp.tool(annotations=_READ_ONLY)
    def index_status() -> dict[str, Any]:
        """Is the local index built, how old is it, how much of it is embedded, and
        is the embedder real or a mock? Call this when results look empty or
        stale."""
        return service.index_status()

    @mcp.tool(annotations=_LOCAL_WRITE)
    async def build_metadata_index(
        ctx: Context,
        component_types: list[str] | None = None,
        full_refresh: bool = False,
    ) -> dict[str, Any]:
        """Build or refresh the local index from the org via the sf CLI (read-only
        against the org; writes only the local index). Incremental by default
        (only changed components); full_refresh=true re-fetches everything. Can
        take a minute or more on a large org. Requires the sf CLI and an
        authenticated org alias."""
        if _sf_cli_path() is None:
            return {
                "error": "sf_cli_missing",
                "hint": (
                    "Install the Salesforce CLI "
                    "(https://developer.salesforce.com/tools/salesforcecli) and run: "
                    f"sf org login web --alias {service.org_alias}"
                ),
            }
        return await _run_with_heartbeat(
            ctx, "Indexing org", service.build_metadata_index,
            component_types=component_types, full_refresh=full_refresh,
        )

    @mcp.tool(annotations=_LOCAL_WRITE)
    async def embed_metadata_index(
        ctx: Context,
        component_types: list[str] | None = None,
        force: bool = False,
        reset_embeddings: bool = False,
    ) -> dict[str, Any]:
        """Compute embeddings for indexed components so semantic_search works.
        Skips unchanged components. Uses the embedder reported by index_status
        (local fastembed by default; set SF_CONTEXT_EMBEDDER to change). If the
        embedder changed since the index was built, this refuses with
        embedder_mismatch; pass reset_embeddings=true to discard old vectors and
        recompute them."""
        return await _run_with_heartbeat(
            ctx, "Embedding components", service.embed_metadata_index,
            component_types=component_types, force=force,
            reset_embeddings=reset_embeddings,
        )

    @mcp.tool(annotations=_LOCAL_WRITE)
    async def embed_knowledge_base(
        ctx: Context,
        category: str | None = None,
        force: bool = False,
        reset_embeddings: bool = False,
    ) -> dict[str, Any]:
        """Load the bundled best-practice entries and embed them so
        knowledge_search and retrieve_context can use them. Run once after
        install."""
        return await _run_with_heartbeat(
            ctx, "Embedding knowledge base", service.embed_knowledge_base,
            category=category, force=force, reset_embeddings=reset_embeddings,
        )

    # ----------------------------------------------------------------- memory

    @mcp.tool(annotations=_MEMORY_WRITE)
    def memory_save(
        type: str,
        name: str,
        description: str,
        body: str,
        tags: list[str] | None = None,
        cross_org: bool = False,
    ) -> dict[str, Any]:
        """Remember something durable about this org or team so later sessions
        (and other agents) can recall it: a decision, preference, constraint or
        pointer. type: user, feedback, project, reference. For project and
        feedback bodies lead with the fact, then 'Why:' and 'How to apply:'
        lines. Saved per (tenant, org); cross_org=true applies it to every org
        in the tenant. Embeddings must be refreshed before it is recallable."""
        return service.memory_save(
            type=type, name=name, description=description, body=body,
            tags=tags, cross_org=cross_org,
        )

    @mcp.tool(annotations=_READ_ONLY)
    def memory_recall(
        query: str, type: str | None = None, limit: int = 5, min_score: float = 0.0,
    ) -> dict[str, Any]:
        """Find saved memories relevant to a query ("how do we handle triggers
        here?"). Check this at the start of a task to pick up prior decisions."""
        return service.memory_recall(query=query, type=type, limit=limit, min_score=min_score)

    @mcp.tool(annotations=_READ_ONLY)
    def memory_list(
        type: str | None = None, limit: int = 25, include_superseded: bool = False,
    ) -> dict[str, Any]:
        """Browse saved memories for this org without a search query (no
        embedding cost)."""
        return service.memory_list(
            type=type, limit=limit, include_superseded=include_superseded,
        )

    return mcp


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="sf-context-mcp",
        description="MCP server for the Salesforce context engine.",
    )
    p.add_argument("--org", default=os.environ.get("SF_ORG_ALIAS"),
                   help="Authenticated sf org alias (env: SF_ORG_ALIAS)")
    p.add_argument("--tenant", default=os.environ.get("SF_CONTEXT_TENANT", "local-dev"),
                   help="Tenant id for memory scoping (env: SF_CONTEXT_TENANT)")
    p.add_argument("--db", default=os.environ.get("SF_CONTEXT_DB"),
                   help="SQLite path (env: SF_CONTEXT_DB; "
                        "default: <SF_CONTEXT_HOME>/metadata_index.db)")
    p.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    p.add_argument("--warmup", action="store_true",
                   help="Download/load the embedding model, then exit")
    return p.parse_args(argv)


def _warmup() -> int:
    """Pre-download the embedding model so the first MCP call doesn't stall."""
    from sf_context_engine import create_embedder

    try:
        embedder = create_embedder()
        warm = getattr(embedder, "warmup", None)
        if warm is not None:
            print(f"Downloading/loading {embedder.name} ...", file=sys.stderr)
            warm()
        print(f"Embedder ready: {embedder.name} ({embedder.dim}-d)", file=sys.stderr)
        return 0
    except (ValueError, ImportError) as exc:
        print(f"sf-context-mcp: embedder unavailable: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    args = _parse_args(argv)
    # Private-by-default: use the local embedder when available instead of
    # sending org code to a hosted API just because a key happens to be in env.
    # Set SF_CONTEXT_EMBEDDER explicitly (e.g. "gemini") to override.
    if importlib.util.find_spec("fastembed") is not None:
        os.environ.setdefault("SF_CONTEXT_EMBEDDER", "fastembed")
    if args.warmup:
        raise SystemExit(_warmup())
    if not args.org:
        print("sf-context-mcp: --org (or SF_ORG_ALIAS) is required", file=sys.stderr)
        raise SystemExit(2)
    service = ContextService(
        org_alias=args.org,
        tenant_id=args.tenant,
        db_path=Path(args.db) if args.db else None,
    )
    build_server(service).run(transport=args.transport)


if __name__ == "__main__":
    main()
