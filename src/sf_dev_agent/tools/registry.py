"""Tool registry — defines, registers, and dispatches tools for the agent.

Each tool is defined as a ToolDefinition (name + JSON schema) and backed
by an executor function. The registry serializes definitions into the format
Claude's API expects for the `tools` parameter, and routes tool_use calls
to the correct executor.

For Week 1, most tools are stubs that shell out to `sf` CLI commands.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sf_dev_agent.memory.working import WorkingMemoryStore
from sf_dev_agent.models.schemas import OrgConnection, ToolDefinition
from sf_dev_agent.paths import agent_workspace

logger = logging.getLogger(__name__)

# SF CLI tools that can be intercepted by mock mode
_SF_TOOLS = frozenset({
    "sf_metadata_describe",
    "sf_soql_query",
    "sf_retrieve",
    "sf_source_deploy",
    "sf_test_run",
    "build_metadata_index",  # also hits the org via sf project retrieve
    # Embedding tools also call out to a remote provider (Gemini), so they're
    # intercepted in mock mode to avoid burning API quota on offline tests.
    "embed_metadata_index",
    "semantic_search",
    "embed_knowledge_base",  # also calls Gemini embeddings
    "knowledge_search",      # embeds the query via Gemini
    "retrieve_context",      # fans out to vector layers; embeds the query via Gemini
    "memory_recall",         # embeds the query via Gemini; save/list are pure local SQLite
})


class ToolRegistry:
    """Manages tool schemas and executors for the agent."""

    def __init__(
        self,
        org: OrgConnection,
        mock_org: bool = False,
        index_db_path: Path | None = None,
        working_memory: WorkingMemoryStore | None = None,
    ) -> None:
        self.org = org
        self.mock_org = mock_org
        self.index_db_path = index_db_path  # None -> default location resolved lazily
        # Optional handle on the running agent's WorkingMemoryStore. The
        # resume tools (list_resumable_tasks, get_task_summary,
        # request_resume) need it; everything else degrades gracefully.
        self.working_memory = working_memory
        self._tools: dict[str, ToolDefinition] = {}
        self._executors: dict[str, Callable[..., Any]] = {}

        if mock_org:
            logger.info("ToolRegistry running in mock-org mode — SF CLI calls are stubbed")

        # Register all built-in tools
        self._register_builtin_tools()

    def register(
        self,
        definition: ToolDefinition,
        executor: Callable[..., Any],
    ) -> None:
        """Register a tool with its definition and executor function."""
        self._tools[definition.name] = definition
        self._executors[definition.name] = executor

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        """Return tool definitions in provider-neutral format.

        Each entry: {"name": str, "description": str, "parameters": dict}
        Provider adapters convert this to their native tool/function format.
        """
        return [
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            }
            for t in self._tools.values()
        ]

    def execute(self, tool_name: str, tool_input: dict[str, Any]) -> Any:
        """Dispatch a tool call to its executor."""
        if tool_name not in self._executors:
            raise ValueError(f"Unknown tool: {tool_name}")

        # In mock-org mode, intercept SF CLI tools and return canned responses.
        if self.mock_org and tool_name in _SF_TOOLS:
            from sf_dev_agent.tools.mock_responses import get_mock_response
            logger.info("MOCK: %s %s", tool_name, tool_input)
            return get_mock_response(tool_name, tool_input)

        return self._executors[tool_name](**tool_input)

    # ------------------------------------------------------------------
    # Built-in tool registration
    # ------------------------------------------------------------------

    def _register_builtin_tools(self) -> None:
        """Register all Week 1 tools."""

        # --- sf_metadata_describe ---
        self.register(
            ToolDefinition(
                name="sf_metadata_describe",
                description=(
                    "Query the Salesforce org's metadata. Returns object definitions, "
                    "field schemas, existing automation (triggers, flows, validation "
                    "rules). Use this to understand what exists in the org before "
                    "making changes."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "component_type": {
                            "type": "string",
                            "description": (
                                "Metadata type: CustomObject, ApexClass, ApexTrigger, "
                                "Flow, ValidationRule, CustomField, PermissionSet, "
                                "LightningComponentBundle, etc."
                            ),
                        },
                        "component_names": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Specific component API names. If omitted, lists all "
                                "of that type."
                            ),
                        },
                    },
                    "required": ["component_type"],
                },
                read_only=True,
            ),
            executor=self._exec_metadata_describe,
        )

        # --- sf_soql_query ---
        self.register(
            ToolDefinition(
                name="sf_soql_query",
                description=(
                    "Execute a read-only SOQL query against the connected org. "
                    "Use bind-variable style for any dynamic values. "
                    "Max 2000 rows returned."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The SOQL query string.",
                        },
                        "max_rows": {
                            "type": "integer",
                            "description": "Max rows to return (default 500, max 2000).",
                            "default": 500,
                        },
                    },
                    "required": ["query"],
                },
                read_only=True,
            ),
            executor=self._exec_soql_query,
        )

        # --- sf_retrieve ---
        self.register(
            ToolDefinition(
                name="sf_retrieve",
                description=(
                    "Pull source code and metadata from the org. Use to examine "
                    "existing Apex classes, triggers, LWCs. Returns file contents."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "components": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Metadata component IDs — e.g., "
                                '["ApexClass:AccountHandler", "ApexTrigger:AccountTrigger"]'
                            ),
                        },
                    },
                    "required": ["components"],
                },
                read_only=True,
            ),
            executor=self._exec_retrieve,
        )

        # --- sf_source_deploy ---
        self.register(
            ToolDefinition(
                name="sf_source_deploy",
                description=(
                    "Deploy source to a Salesforce org. WRITE OPERATION — requires "
                    "approved plan. Use --dry-run during planning."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "source_path": {
                            "type": "string",
                            "description": "Path to source directory or files to deploy.",
                        },
                        "test_level": {
                            "type": "string",
                            "enum": [
                                "NoTestRun",
                                "RunSpecifiedTests",
                                "RunLocalTests",
                                "RunAllTests",
                            ],
                            "description": "Test level for deployment.",
                            "default": "RunSpecifiedTests",
                        },
                        "specified_tests": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Test class names (for RunSpecifiedTests).",
                        },
                        "dry_run": {
                            "type": "boolean",
                            "description": "Validate without deploying.",
                            "default": False,
                        },
                    },
                    "required": ["source_path"],
                },
                read_only=False,
            ),
            executor=self._exec_source_deploy,
        )

        # --- sf_test_run ---
        self.register(
            ToolDefinition(
                name="sf_test_run",
                description=(
                    "Run Apex tests on the connected org. Returns pass/fail, "
                    "code coverage, and assertion failures."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "test_classes": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Test class names to execute.",
                        },
                    },
                    "required": ["test_classes"],
                },
                read_only=False,
            ),
            executor=self._exec_test_run,
        )

        # --- file_write ---
        self.register(
            ToolDefinition(
                name="file_write",
                description=(
                    "Create or modify a file in the local project workspace. "
                    "WRITE OPERATION — requires approved plan."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Path relative to project workspace.",
                        },
                        "content": {
                            "type": "string",
                            "description": "Full file content.",
                        },
                    },
                    "required": ["file_path", "content"],
                },
                read_only=False,
            ),
            executor=self._exec_file_write,
        )

        # --- file_read ---
        self.register(
            ToolDefinition(
                name="file_read",
                description="Read a file from the local project workspace.",
                parameters={
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Path relative to project workspace.",
                        },
                    },
                    "required": ["file_path"],
                },
                read_only=True,
            ),
            executor=self._exec_file_read,
        )

        # --- bash ---
        self.register(
            ToolDefinition(
                name="bash",
                description=(
                    "Execute a shell command. Available: sf CLI, node, npm, git. "
                    "WRITE OPERATION — requires approved plan for mutating commands."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "The command to execute.",
                        },
                        "description": {
                            "type": "string",
                            "description": "5-10 word description of what this does.",
                        },
                        "timeout": {
                            "type": "integer",
                            "description": "Timeout in seconds (default 240).",
                            "default": 240,
                        },
                    },
                    "required": ["command", "description"],
                },
                read_only=False,
            ),
            executor=self._exec_bash,
        )

        # --- retrieve_context (orchestrator: all three layers in one call) ---
        self.register(
            ToolDefinition(
                name="retrieve_context",
                description=(
                    "PREFERRED for open-ended exploration. One call fans out "
                    "to all three context layers (semantic + literal code "
                    "search + knowledge base), graph-enriches the top code "
                    "hits, dedupes, and returns a focused token-budgeted "
                    "payload with provenance.\n\n"
                    "Use this when the question is broad ('what do we know "
                    "about duplicate detection?', 'how should I structure "
                    "the trigger handler?') and you don't yet know which "
                    "layer holds the answer.\n\n"
                    "When you DO know the layer (a literal id lookup, a "
                    "specific dependency walk, or a targeted governor-limit "
                    "lookup), the per-layer tools (code_search, "
                    "sf_dependency_graph, semantic_search, knowledge_search) "
                    "are cheaper and more precise. Costs one Gemini "
                    "embedding call (the query is embedded once and reused "
                    "across vector layers)."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Natural-language description of the context you need.",
                        },
                        "max_tokens": {
                            "type": "integer",
                            "description": (
                                "Total token budget for the assembled "
                                "payload (default 4000)."
                            ),
                            "default": 4000,
                        },
                        "max_per_layer": {
                            "type": "integer",
                            "description": (
                                "Per-layer hit cap before dedupe "
                                "(default 6). Higher values cast a wider "
                                "net but eat more of the budget."
                            ),
                            "default": 6,
                        },
                        "enrich_top_k": {
                            "type": "integer",
                            "description": (
                                "Number of top code hits to graph-enrich "
                                "with one-hop relationship edges. Default "
                                "3; pass 0 to disable."
                            ),
                            "default": 3,
                        },
                        "component_type": {
                            "type": "string",
                            "description": (
                                "Restrict semantic + literal layers to "
                                "this component type. Omit for all."
                            ),
                        },
                        "knowledge_category": {
                            "type": "string",
                            "enum": [
                                "governor_limit",
                                "anti_pattern",
                                "best_practice",
                                "pattern",
                            ],
                            "description": "Restrict knowledge layer to one category.",
                        },
                        "memory_type": {
                            "type": "string",
                            "enum": ["user", "feedback", "project", "reference"],
                            "description": (
                                "Restrict memory layer to one type. Memories "
                                "are scoped to the current tenant + org "
                                "automatically."
                            ),
                        },
                    },
                    "required": ["query"],
                },
                read_only=True,
            ),
            executor=self._exec_retrieve_context,
        )

        # --- code_search (metadata index) ---
        self.register(
            ToolDefinition(
                name="code_search",
                description=(
                    "Search the local SQLite metadata index for components by name "
                    "or source-text substring. Cheap and deterministic — prefer this "
                    "over sf_metadata_describe / sf_retrieve when answering 'what "
                    "exists?' questions. Returns id, type, api_name, and a metadata "
                    "summary; pass include_source=true to include the full source "
                    "(can be large). Build the index first with build_metadata_index "
                    "if it's empty."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Substring to match against api_name and source.",
                        },
                        "component_type": {
                            "type": "string",
                            "description": (
                                "Restrict to one type (ApexClass, ApexTrigger, "
                                "CustomObject, CustomField, ...). Omit for all types."
                            ),
                        },
                        "include_source": {
                            "type": "boolean",
                            "description": "Include full source text in results (default false).",
                            "default": False,
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max results to return (default 25, max 100).",
                            "default": 25,
                        },
                        "source_max_lines": {
                            "type": "integer",
                            "description": (
                                "When include_source=true, trim each returned "
                                "source to this many head lines (default ~80) "
                                "with a `… (N more lines, full body at <id>)` "
                                "suffix. Pass 0 for untrimmed bodies."
                            ),
                            "default": 80,
                        },
                    },
                    "required": ["query"],
                },
                read_only=True,
            ),
            executor=self._exec_code_search,
        )

        # --- sf_dependency_graph (metadata index) ---
        self.register(
            ToolDefinition(
                name="sf_dependency_graph",
                description=(
                    "Return the relationship edges touching a component in the local "
                    "metadata index — what it triggers on, what fields it has, what "
                    "it extends/implements, and what depends on it. Use this to "
                    "understand the blast radius of a change before modifying a "
                    "component."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "component_id": {
                            "type": "string",
                            "description": (
                                "Canonical id like 'ApexTrigger:AccountTrigger' or "
                                "'CustomObject:Account'. If you only have a name, "
                                "use component_type + api_name instead."
                            ),
                        },
                        "component_type": {
                            "type": "string",
                            "description": "Used with api_name when component_id is unknown.",
                        },
                        "api_name": {
                            "type": "string",
                            "description": "Used with component_type when component_id is unknown.",
                        },
                        "direction": {
                            "type": "string",
                            "enum": ["outgoing", "incoming", "both"],
                            "description": "Which edges to return (default both).",
                            "default": "both",
                        },
                    },
                },
                read_only=True,
            ),
            executor=self._exec_sf_dependency_graph,
        )

        # --- knowledge_search (vector search over the bundled knowledge base) ---
        self.register(
            ToolDefinition(
                name="knowledge_search",
                description=(
                    "Vector-based search over the bundled Salesforce knowledge "
                    "base — governor limits, anti-patterns, best practices, and "
                    "architectural patterns. Use this when you need PLATFORM "
                    "knowledge that's not org-specific: 'is SOQL in a loop OK?', "
                    "'what's the heap size limit?', 'how should I structure "
                    "trigger handlers?'. For org-specific code questions use "
                    "code_search / semantic_search instead."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Natural-language description of what you want to know.",
                        },
                        "category": {
                            "type": "string",
                            "enum": [
                                "governor_limit",
                                "anti_pattern",
                                "best_practice",
                                "pattern",
                            ],
                            "description": "Restrict to one category. Omit for all.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max results (default 5, max 25).",
                            "default": 5,
                        },
                        "min_score": {
                            "type": "number",
                            "description": (
                                "Drop results below this cosine-similarity "
                                "threshold (0..1). Default 0 (return all top-k)."
                            ),
                            "default": 0.0,
                        },
                    },
                    "required": ["query"],
                },
                read_only=True,
            ),
            executor=self._exec_knowledge_search,
        )

        # --- memory_save (write a project-memory row) -------------------
        self.register(
            ToolDefinition(
                name="memory_save",
                description=(
                    "Persist a memory across sessions. Use the four-type "
                    "taxonomy ported from Claude Code's auto-memory:\n"
                    "  - user:      facts about the human (role, prefs, knowledge)\n"
                    "  - feedback:  corrections AND validated non-obvious choices\n"
                    "  - project:   ongoing work, decisions, deadlines\n"
                    "  - reference: pointers to external systems (Linear, Grafana, etc.)\n\n"
                    "Save WHEN you learn something durable that future sessions "
                    "should know. Body convention: rule first, then a `**Why:**` "
                    "line and a `**How to apply:**` line so future-you can judge "
                    "edge cases. Memories scope to the current (tenant, org) "
                    "automatically; pass `cross_org=True` to make a memory "
                    "tenant-wide (no org pinning)."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["user", "feedback", "project", "reference"],
                        },
                        "name": {
                            "type": "string",
                            "description": (
                                "Short human-friendly handle (e.g., "
                                "'merge-freeze-2026-03-05'). Used in citations."
                            ),
                        },
                        "description": {
                            "type": "string",
                            "description": (
                                "One-line relevance hook. Used at recall time "
                                "to decide if this memory matters for the "
                                "current task. Be specific."
                            ),
                        },
                        "body": {
                            "type": "string",
                            "description": (
                                "The memory content. For feedback / project: "
                                "rule, then `**Why:**`, then `**How to apply:**`."
                            ),
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional tags for filtering / future search.",
                        },
                        "cross_org": {
                            "type": "boolean",
                            "description": (
                                "If true, store with org_alias=NULL so the "
                                "memory applies to every org under this tenant. "
                                "Default false: scope to the current org."
                            ),
                            "default": False,
                        },
                    },
                    "required": ["type", "name", "description", "body"],
                },
                read_only=False,
            ),
            executor=self._exec_memory_save,
        )

        # --- memory_recall (vector search over saved memories) -----------
        self.register(
            ToolDefinition(
                name="memory_recall",
                description=(
                    "Retrieve memories relevant to the current task. Cosine-"
                    "ranked over the embedding of `query`. Scoped to the "
                    "current (tenant, org_alias OR NULL) automatically. "
                    "Use this BEFORE making decisions about how to approach a "
                    "task — past feedback, user preferences, and project "
                    "context live here. Costs one Gemini embedding call."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "What you want to recall context for.",
                        },
                        "type": {
                            "type": "string",
                            "enum": ["user", "feedback", "project", "reference"],
                            "description": "Restrict to one memory type. Omit for all.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max results (default 5, max 25).",
                            "default": 5,
                        },
                        "min_score": {
                            "type": "number",
                            "description": (
                                "Drop hits below this cosine threshold (0..1). "
                                "Default 0 (return all top-k)."
                            ),
                            "default": 0.0,
                        },
                    },
                    "required": ["query"],
                },
                read_only=True,
            ),
            executor=self._exec_memory_recall,
        )

        # --- memory_list (browse without embedding the query) ------------
        self.register(
            ToolDefinition(
                name="memory_list",
                description=(
                    "List memories in the current scope (tenant + org), newest "
                    "first. No embedding cost — use this when you want to see "
                    "what's stored, not search by meaning."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["user", "feedback", "project", "reference"],
                            "description": "Restrict to one memory type. Omit for all.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max rows (default 25, max 100).",
                            "default": 25,
                        },
                        "include_superseded": {
                            "type": "boolean",
                            "description": (
                                "Include memories that have been replaced by a "
                                "newer one (compaction). Default false."
                            ),
                            "default": False,
                        },
                    },
                },
                read_only=True,
            ),
            executor=self._exec_memory_list,
        )

        # --- memory_compact (find merge candidates) ----------------------
        self.register(
            ToolDefinition(
                name="memory_compact",
                description=(
                    "Detect clusters of similar memories that could be "
                    "consolidated. Pairwise cosine within (scope, type); "
                    "rows whose similarity is at or above `threshold` are "
                    "grouped via connected components. Returns clusters of "
                    "size 2+ ordered by tightness.\n\n"
                    "Use this when memory_list shows the working set "
                    "growing past ~30-50 entries, or after a session that "
                    "produced several near-duplicate feedback memories.\n\n"
                    "Read-only — no Gemini call (operates on stored "
                    "embeddings). To act on a cluster: write a single "
                    "consolidated memory with memory_save, then call "
                    "memory_supersede for each old member pointing at the "
                    "new memory's id."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["user", "feedback", "project", "reference"],
                            "description": (
                                "Restrict clustering to one memory type. "
                                "Omit to scan all four types (clusters are "
                                "always type-homogeneous regardless)."
                            ),
                        },
                        "threshold": {
                            "type": "number",
                            "description": (
                                "Cosine cutoff for 'these are the same "
                                "idea' (0..1). Default 0.85 — high enough "
                                "that genuinely different memories don't "
                                "cluster, low enough that paraphrases do."
                            ),
                            "default": 0.85,
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max clusters returned (default 10, max 25).",
                            "default": 10,
                        },
                    },
                },
                read_only=True,
            ),
            executor=self._exec_memory_compact,
        )

        # --- check_index_freshness (read-only freshness probe) -----------
        self.register(
            ToolDefinition(
                name="check_index_freshness",
                description=(
                    "Re-check how recently the metadata index was built "
                    "for the current org and how much of it is embedded. "
                    "Read-only, pure local SQLite — no Gemini call, no "
                    "org call. Use this if you suspect the index is "
                    "stale mid-task and want to confirm before deciding "
                    "to call build_metadata_index --delta.\n\n"
                    "Returns: last_built_at (ISO timestamp or null), "
                    "age_seconds, is_stale (bool), embedding_coverage_pct, "
                    "components_count, embedded_count, last_run_error, "
                    "freshness_line (the same one-liner that's pinned in "
                    "your env block at session start)."
                ),
                parameters={
                    "type": "object",
                    "properties": {},
                },
                read_only=True,
            ),
            executor=self._exec_check_index_freshness,
        )

        # --- memory_supersede (link old memory -> newer merged memory) ---
        self.register(
            ToolDefinition(
                name="memory_supersede",
                description=(
                    "Mark `old_id` as superseded by `new_id`. The old row "
                    "stays in the database (audit trail) but disappears "
                    "from recall and list (use include_superseded=true to "
                    "see them again). Pair with memory_save to ship a "
                    "consolidated memory, then supersede each old member.\n\n"
                    "Both ids must already exist. Cross-scope supersede is "
                    "allowed but typically a mistake — usually you want "
                    "old and new in the same (tenant, org_alias)."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "old_id": {
                            "type": "string",
                            "description": "ID of the memory being replaced.",
                        },
                        "new_id": {
                            "type": "string",
                            "description": (
                                "ID of the memory replacing it. Must "
                                "exist (write the merged memory first "
                                "with memory_save)."
                            ),
                        },
                    },
                    "required": ["old_id", "new_id"],
                },
                read_only=False,
            ),
            executor=self._exec_memory_supersede,
        )

        # --- embed_knowledge_base (auto-loads + embeds bundled entries) ---
        self.register(
            ToolDefinition(
                name="embed_knowledge_base",
                description=(
                    "Auto-load the bundled knowledge entries (if not already "
                    "loaded) and populate/refresh their embeddings. "
                    "Hash-gated — only re-embeds entries whose source text "
                    "actually changed. Run once at session start when "
                    "knowledge_search is going to be used; cheap to re-run."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "category": {
                            "type": "string",
                            "description": "Restrict to one category.",
                        },
                        "force": {
                            "type": "boolean",
                            "description": "Re-embed even unchanged entries.",
                            "default": False,
                        },
                    },
                },
                read_only=True,
            ),
            executor=self._exec_embed_knowledge_base,
        )

        # --- semantic_search (vector search over the metadata index) ---
        self.register(
            ToolDefinition(
                name="semantic_search",
                description=(
                    "Vector-based semantic search over the metadata index. "
                    "Use this when you're looking for code or components by "
                    "concept rather than literal name — e.g., 'duplicate "
                    "detection logic', 'tax calculation', 'lead routing'. "
                    "Prefer code_search for literal name/substring lookups; "
                    "prefer this for conceptual queries. Requires that "
                    "embed_metadata_index has been run; returns a structured "
                    "error if no embeddings exist yet."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Natural-language description of what you're looking for.",
                        },
                        "component_type": {
                            "type": "string",
                            "description": "Restrict to one type. Omit for all.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max results (default 10, max 50).",
                            "default": 10,
                        },
                        "min_score": {
                            "type": "number",
                            "description": (
                                "Drop results below this cosine-similarity "
                                "threshold (0..1). Default 0 (return all top-k)."
                            ),
                            "default": 0.0,
                        },
                    },
                    "required": ["query"],
                },
                read_only=True,
            ),
            executor=self._exec_semantic_search,
        )

        # --- embed_metadata_index (populate/refresh embeddings) ---
        self.register(
            ToolDefinition(
                name="embed_metadata_index",
                description=(
                    "Populate or refresh embeddings for components in the "
                    "local metadata index. Hash-gated — only re-embeds rows "
                    "whose source has changed since last embedding. Run this "
                    "once after build_metadata_index, and again after deploys "
                    "that change source. Cheap when nothing has changed."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "component_types": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Restrict re-embedding to these types. "
                                "Omit to refresh all supported types."
                            ),
                        },
                        "force": {
                            "type": "boolean",
                            "description": (
                                "Re-embed even if the source hash is "
                                "unchanged. Use after switching embedder "
                                "models. Default false."
                            ),
                            "default": False,
                        },
                    },
                },
                read_only=True,
            ),
            executor=self._exec_embed_metadata_index,
        )

        # --- build_metadata_index (refreshes local SQLite from live org) ---
        self.register(
            ToolDefinition(
                name="build_metadata_index",
                description=(
                    "Refresh the local SQLite metadata index from the connected "
                    "org. Read-only against the org. Currently indexes ApexClass, "
                    "ApexTrigger, CustomObject (and their CustomFields).\n\n"
                    "Defaults to **delta-refresh**: only components whose "
                    "LastModifiedDate in the org is newer than what's in the "
                    "local index are retrieved, components no longer in the org "
                    "are pruned, and unchanged components are skipped. This "
                    "makes post-deploy refreshes cheap. ApexClass and ApexTrigger "
                    "support delta; other types fall back to full retrieve.\n\n"
                    "Pass full_refresh=true as a 'rebuild from scratch' escape "
                    "hatch (e.g. after a parser/schema change)."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "component_types": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Restrict the refresh to these types (default: all "
                                "supported types). Useful for fast post-deploy refreshes."
                            ),
                        },
                        "full_refresh": {
                            "type": "boolean",
                            "description": (
                                "Bypass delta logic and re-fetch every component "
                                "for the requested types. Default false."
                            ),
                            "default": False,
                        },
                    },
                },
                read_only=True,
            ),
            executor=self._exec_build_metadata_index,
        )

        # --- list_resumable_tasks (read-only browse of working memory) ---
        self.register(
            ToolDefinition(
                name="list_resumable_tasks",
                description=(
                    "List in-flight tasks the user could resume — anything "
                    "in working memory whose status is NOT terminal "
                    "(complete / failed / rolled_back). Scoped to the "
                    "current (tenant, org) automatically. Use this when "
                    "the user asks 'what was I working on?' or signals "
                    "they want to pick something back up.\n\n"
                    "Returns task ids, statuses, abbreviated user requests, "
                    "and rough message counts so you can summarize options. "
                    "If you need the transcript head for a specific task, "
                    "follow up with get_task_summary(task_id)."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "Max rows (default 10, max 50).",
                            "default": 10,
                        },
                        "include_terminal": {
                            "type": "boolean",
                            "description": (
                                "If true, also include completed/failed "
                                "tasks (read-only history view). Default "
                                "false — resumable tasks only."
                            ),
                            "default": False,
                        },
                    },
                },
                read_only=True,
            ),
            executor=self._exec_list_resumable_tasks,
        )

        # --- get_task_summary (read transcript head for one task) -------
        self.register(
            ToolDefinition(
                name="get_task_summary",
                description=(
                    "Return the head of one task's conversation transcript "
                    "plus its plan + status. Use this to confirm with the "
                    "user that you're picking up the right task before "
                    "calling request_resume.\n\n"
                    "Truncates message bodies aggressively — this is a "
                    "summary, not a full replay."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "task_id": {
                            "type": "string",
                            "description": "Task id from list_resumable_tasks.",
                        },
                        "max_messages": {
                            "type": "integer",
                            "description": "How many transcript rows to include (default 6, max 25).",
                            "default": 6,
                        },
                    },
                    "required": ["task_id"],
                },
                read_only=True,
            ),
            executor=self._exec_get_task_summary,
        )

        # --- request_resume (intercepted — REPL takes over and resumes) -
        # Schema exposed to the LLM; the AgentLoop intercepts the call and
        # signals back to the REPL. This executor is never invoked.
        self.register(
            ToolDefinition(
                name="request_resume",
                description=(
                    "Hand off control to the user's REPL to resume the "
                    "named task. Call this ONLY after you've confirmed "
                    "(via list_resumable_tasks + the user's intent) that "
                    "this is the task they want to continue.\n\n"
                    "After this call, the current agent run ends and the "
                    "REPL re-invokes AgentLoop.resume(task_id) to pick up "
                    "where the prior session left off — using the saved "
                    "transcript, plan, and approval state. Don't keep "
                    "talking after calling this; just confirm briefly and "
                    "stop."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "task_id": {
                            "type": "string",
                            "description": "ID of the task to resume.",
                        },
                        "rationale": {
                            "type": "string",
                            "description": (
                                "One sentence explaining why this task "
                                "matches the user's request. Useful for "
                                "the operator log."
                            ),
                        },
                    },
                    "required": ["task_id"],
                },
                read_only=True,
            ),
            executor=lambda **_: {"intercepted": True},  # never reached
        )

        # --- request_user_input (intercepted — REPL prompts the user) ----
        # Slice 4 of the PI-style refactor. The LLM calls this when it
        # needs a clarifying answer mid-execution. The AgentLoop
        # intercepts the call, persists the question, breaks the loop,
        # and the REPL (or default driver) prompts the user. The answer
        # feeds back via `agent.queue_follow_up()`.
        self.register(
            ToolDefinition(
                name="request_user_input",
                description=(
                    "Ask the user a clarifying question mid-task and "
                    "wait for an answer. The agent loop pauses; the "
                    "REPL prompts the user; the answer comes back as "
                    "the next user message. Use this instead of asking "
                    "in free-form prose, which the REPL can't reliably "
                    "route back into the active run.\n\n"
                    "Examples: 'Which org should I deploy to?', "
                    "'You have three RecordSelectorController versions; "
                    "which one should I update?'. Keep the question "
                    "short and answerable in one line."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": (
                                "The question to put to the user. "
                                "Single-line, plain text."
                            ),
                        },
                        "choices": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Optional fixed-choice answers (e.g. "
                                "['yes', 'no']). If supplied, the REPL "
                                "renders them as a constrained prompt."
                            ),
                        },
                    },
                    "required": ["question"],
                },
                read_only=True,
            ),
            executor=lambda **_: {"intercepted": True},  # never reached
        )

        # --- submit_plan ---
        # Schema exposed to the LLM; execution is intercepted by AgentLoop
        # before it reaches this registry — this executor is never called.
        self.register(
            ToolDefinition(
                name="submit_plan",
                description=(
                    "MANDATORY: Call this at the end of Phase 1 to register the "
                    "structured execution plan and trigger the user approval gate. "
                    "The agent cannot proceed to execution until this is called."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "summary": {
                            "type": "string",
                            "description": "1-2 sentence description of what will be done and why.",
                        },
                        "steps": {
                            "type": "array",
                            "description": "Ordered list of execution steps.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "step_number": {"type": "integer"},
                                    "action": {
                                        "type": "string",
                                        "description": "Tool name: file_write, sf_source_deploy, etc.",
                                    },
                                    "target": {
                                        "type": "string",
                                        "description": "Target file path or component API name.",
                                    },
                                    "mode": {
                                        "type": "string",
                                        "enum": ["read", "create", "modify", "delete"],
                                    },
                                    "risk": {
                                        "type": "string",
                                        "enum": ["none", "low", "medium", "high"],
                                    },
                                    "description": {"type": "string"},
                                },
                                "required": [
                                    "step_number", "action", "target",
                                    "mode", "description",
                                ],
                            },
                        },
                        "risk_assessment": {
                            "type": "string",
                            "enum": ["none", "low", "medium", "high"],
                        },
                        "risk_reasoning": {"type": "string"},
                        "rollback_strategy": {"type": "string"},
                        "preflight_checks": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string"},
                                    "result": {"type": "string"},
                                },
                                "required": ["label", "result"],
                            },
                        },
                        "components_created":  {"type": "integer", "default": 0},
                        "components_modified": {"type": "integer", "default": 0},
                        "components_deleted":  {"type": "integer", "default": 0},
                        "test_classes_affected": {"type": "integer", "default": 0},
                    },
                    "required": ["summary", "steps", "risk_assessment", "rollback_strategy"],
                },
                read_only=True,
            ),
            executor=lambda **_: {"registered": True},  # intercepted by AgentLoop
        )

    # ------------------------------------------------------------------
    # Tool executors (Week 1: shell out to sf CLI)
    # ------------------------------------------------------------------

    def _run_sf_cli(self, args: list[str], timeout: int = 240) -> dict[str, Any]:
        """Run an sf CLI command from the agent workspace and return parsed JSON."""
        workspace = agent_workspace()

        # On Windows, sf is installed as sf.cmd — bare "sf" only works with shell=True
        sf_exe = "sf.cmd" if sys.platform == "win32" else "sf"
        cmd = [sf_exe] + args + ["--json", "--target-org", self.org.org_alias]
        logger.info("Running (cwd=%s): %s", workspace, " ".join(cmd))

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(workspace),
            )
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError:
                return {
                    "status": result.returncode,
                    "stdout": result.stdout[-5000:],  # truncate
                    "stderr": result.stderr[-2000:],
                }
        except subprocess.TimeoutExpired:
            return {"error": f"Command timed out after {timeout}s"}

    def _exec_metadata_describe(
        self, component_type: str, component_names: list[str] | None = None
    ) -> dict[str, Any]:
        """Describe metadata components in the org.

        - With component_names: retrieve those specific components' source.
        - Without: list every instance of `component_type` that exists in the org.
        """
        if component_names:
            components_arg = ",".join(
                f"{component_type}:{name}" for name in component_names
            )
            return self._run_sf_cli([
                "project", "retrieve", "start",
                "--metadata", components_arg,
            ])
        return self._run_sf_cli([
            "org", "list", "metadata",
            "--metadata-type", component_type,
        ])

    def _exec_soql_query(
        self, query: str, max_rows: int = 500
    ) -> dict[str, Any]:
        """Execute a SOQL query."""
        max_rows = min(max_rows, 2000)
        return self._run_sf_cli([
            "data", "query",
            "--query", query,
            "--result-format", "json",
        ])

    def _exec_retrieve(self, components: list[str]) -> dict[str, Any]:
        """Retrieve source from the org."""
        metadata_arg = ",".join(components)
        return self._run_sf_cli([
            "project", "retrieve", "start",
            "--metadata", metadata_arg,
        ])

    def _exec_source_deploy(
        self,
        source_path: str,
        test_level: str = "RunSpecifiedTests",
        specified_tests: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Deploy source to the org."""
        args = ["project", "deploy", "start", "--source-dir", source_path]

        if test_level:
            args += ["--test-level", test_level]
        if specified_tests:
            args += ["--tests", ",".join(specified_tests)]
        if dry_run:
            args.append("--dry-run")

        return self._run_sf_cli(args, timeout=600)

    def _exec_test_run(self, test_classes: list[str]) -> dict[str, Any]:
        """Run Apex tests."""
        args = [
            "apex", "run", "test",
            "--tests", ",".join(test_classes),
            "--code-coverage",
            "--result-format", "json",
            "--wait", "10",
        ]
        return self._run_sf_cli(args, timeout=600)

    def _exec_file_write(self, file_path: str, content: str) -> dict[str, Any]:
        """Write a file to the workspace."""
        workspace = agent_workspace()
        target = (workspace / file_path).resolve()

        if not str(target).startswith(str(workspace.resolve())):
            return {"error": "Path traversal detected — file_path must be within workspace"}

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

        return {"success": True, "path": str(target), "bytes": len(content.encode())}

    def _exec_file_read(self, file_path: str) -> dict[str, Any]:
        """Read a file from the workspace."""
        workspace = agent_workspace()
        target = (workspace / file_path).resolve()

        if not str(target).startswith(str(workspace.resolve())):
            return {"error": "Path traversal detected — file_path must be within workspace"}

        if not target.exists():
            return {"error": f"File not found: {file_path}"}

        content = target.read_text(encoding="utf-8")
        return {"path": str(target), "content": content, "lines": content.count("\n") + 1}

    def _exec_bash(
        self, command: str, description: str = "", timeout: int = 240
    ) -> dict[str, Any]:
        """Execute a shell command in the agent workspace."""
        workspace = agent_workspace()
        logger.info("Bash [%s] (cwd=%s): %s", description, workspace, command)

        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(workspace),
            )
            return {
                "exit_code": result.returncode,
                "stdout": result.stdout[-10000:],
                "stderr": result.stderr[-5000:],
            }
        except subprocess.TimeoutExpired:
            return {"error": f"Command timed out after {timeout}s"}

    # ------------------------------------------------------------------
    # Metadata-index-backed tools
    # ------------------------------------------------------------------

    def _resolve_index_db_path(self) -> Path:
        """Return the configured DB path, or the package default."""
        if self.index_db_path:
            return self.index_db_path
        from sf_context_engine import default_db_path
        return default_db_path()

    def _memory_scope(self, cross_org: bool = False) -> Any:
        """MemoryScope for the current (tenant, org); cross_org drops the org pin."""
        from sf_dev_agent.memory import MemoryScope

        return MemoryScope(
            tenant_id=self.org.tenant_id,
            org_alias=None if cross_org else self.org.org_alias,
        )

    def _context(self) -> Any:
        """Context-engine service bound to this registry's org + DB."""
        from sf_context_engine.service import ContextService

        return ContextService(
            org_alias=self.org.org_alias,
            tenant_id=self.org.tenant_id,
            db_path=self._resolve_index_db_path(),
        )





    def _exec_code_search(self, **kwargs: Any) -> dict[str, Any]:
        return self._context().code_search(**kwargs)

    def _exec_sf_dependency_graph(self, **kwargs: Any) -> dict[str, Any]:
        return self._context().dependency_graph(**kwargs)

    def _exec_build_metadata_index(self, **kwargs: Any) -> dict[str, Any]:
        return self._context().build_metadata_index(**kwargs)

    def _exec_embed_metadata_index(self, **kwargs: Any) -> dict[str, Any]:
        return self._context().embed_metadata_index(**kwargs)

    # ------------------------------------------------------------------
    # Knowledge-base-backed tools
    # ------------------------------------------------------------------

    def _exec_embed_knowledge_base(self, **kwargs: Any) -> dict[str, Any]:
        return self._context().embed_knowledge_base(**kwargs)

    def _exec_knowledge_search(self, **kwargs: Any) -> dict[str, Any]:
        return self._context().knowledge_search(**kwargs)

    # ------------------------------------------------------------------
    # Memory tier (Wave 8 slice 1)
    # ------------------------------------------------------------------


    def _exec_memory_save(self, **kwargs: Any) -> dict[str, Any]:
        return self._context().memory_save(**kwargs)

    def _exec_memory_recall(self, **kwargs: Any) -> dict[str, Any]:
        return self._context().memory_recall(**kwargs)

    def _exec_memory_list(self, **kwargs: Any) -> dict[str, Any]:
        return self._context().memory_list(**kwargs)

    def _exec_check_index_freshness(self, **kwargs: Any) -> dict[str, Any]:
        return self._context().check_index_freshness(**kwargs)

    def _exec_memory_compact(self, **kwargs: Any) -> dict[str, Any]:
        return self._context().memory_compact(**kwargs)

    def _exec_memory_supersede(self, **kwargs: Any) -> dict[str, Any]:
        return self._context().memory_supersede(**kwargs)

    def _exec_retrieve_context(self, **kwargs: Any) -> dict[str, Any]:
        return self._context().retrieve_context(**kwargs)

    def _exec_semantic_search(self, **kwargs: Any) -> dict[str, Any]:
        return self._context().semantic_search(**kwargs)

    # ------------------------------------------------------------------
    # Resume-by-LLM-intent (Phase C.4)
    # ------------------------------------------------------------------

    def _resume_unavailable(self) -> dict[str, Any]:
        """Shared error for the read-only resume tools when WM is missing."""
        return {
            "error": (
                "No working memory store is attached to this agent run, "
                "so resumable tasks can't be inspected. This usually means "
                "the agent was constructed without persistence — start the "
                "REPL again or pass --working-memory."
            ),
            "tasks": [],
        }

    def _exec_list_resumable_tasks(
        self,
        limit: int = 10,
        include_terminal: bool = False,
    ) -> dict[str, Any]:
        """List tasks the user could resume, scoped to the current org."""
        from sf_dev_agent.memory.working import TERMINAL_STATUSES

        if self.working_memory is None:
            return self._resume_unavailable()

        limit = max(1, min(limit, 50))
        scope = self._memory_scope()

        try:
            rows = self.working_memory.list_tasks(scope=scope, limit=limit * 2)
        except Exception as exc:
            return {"error": f"list_tasks failed: {type(exc).__name__}: {exc}"}

        if not include_terminal:
            rows = [r for r in rows if r.status not in TERMINAL_STATUSES]

        rows = rows[:limit]

        tasks_out: list[dict[str, Any]] = []
        for r in rows:
            try:
                msg_count = self.working_memory.message_count(r.id)
            except Exception:
                msg_count = -1
            tasks_out.append({
                "task_id": r.id,
                "status": r.status,
                "user_request": (
                    r.user_request[:200] + "..."
                    if len(r.user_request) > 200 else r.user_request
                ),
                "plan_approved": r.plan_approved,
                "has_plan": r.plan_json is not None,
                "messages": msg_count,
                "updated_at": r.updated_at,
            })

        return {
            "tenant_id": scope.tenant_id,
            "org_alias": scope.org_alias,
            "include_terminal": include_terminal,
            "count": len(tasks_out),
            "tasks": tasks_out,
            "note": (
                "Use get_task_summary(task_id) for transcript context, "
                "then request_resume(task_id) to hand off to the REPL."
            ) if tasks_out else (
                "No resumable tasks in scope. The user has no in-flight "
                "work to pick up under this org."
            ),
        }

    def _exec_get_task_summary(
        self,
        task_id: str,
        max_messages: int = 6,
    ) -> dict[str, Any]:
        """Return a concise summary of one persisted task."""
        if self.working_memory is None:
            return self._resume_unavailable()

        max_messages = max(1, min(max_messages, 25))
        row = self.working_memory.get_task(task_id)
        if row is None:
            return {"error": f"task {task_id!r} not found"}

        scope = self._memory_scope()
        if row.tenant_id != scope.tenant_id:
            return {
                "error": (
                    f"task {task_id!r} belongs to a different tenant; "
                    "refusing to summarize."
                ),
            }

        try:
            messages = self.working_memory.load_messages(task_id)
        except Exception as exc:
            return {"error": f"load_messages failed: {type(exc).__name__}: {exc}"}

        # Compact each message: role + truncated string repr of content.
        head = messages[:max_messages]
        compact: list[dict[str, Any]] = []
        for m in head:
            content = m.get("content")
            if isinstance(content, str):
                snippet = content[:300] + ("..." if len(content) > 300 else "")
            else:
                # Lists of typed blocks (assistant / tool_result). Pull the
                # first text block if there is one; otherwise type names.
                snippet_parts: list[str] = []
                for block in content if isinstance(content, list) else []:
                    btype = block.get("type") if isinstance(block, dict) else None
                    if btype == "text":
                        text = block.get("text", "")
                        snippet_parts.append(text[:200])
                    elif btype == "tool_use":
                        snippet_parts.append(
                            f"[tool_use {block.get('name', '?')}]"
                        )
                    elif btype == "tool_result":
                        snippet_parts.append("[tool_result]")
                    else:
                        snippet_parts.append(f"[{btype or 'unknown'}]")
                snippet = " | ".join(snippet_parts)[:300]
            compact.append({"role": m["role"], "content": snippet})

        return {
            "task_id": row.id,
            "status": row.status,
            "user_request": row.user_request,
            "plan_approved": row.plan_approved,
            "has_plan": row.plan_json is not None,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "completed_at": row.completed_at,
            "error": row.error,
            "message_count": len(messages),
            "head": compact,
            "truncated": len(messages) > max_messages,
        }
