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
from pathlib import Path
from typing import Any, Callable

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
})


class ToolRegistry:
    """Manages tool schemas and executors for the agent."""

    def __init__(
        self,
        org: OrgConnection,
        mock_org: bool = False,
        index_db_path: Path | None = None,
    ) -> None:
        self.org = org
        self.mock_org = mock_org
        self.index_db_path = index_db_path  # None -> default location resolved lazily
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
        from sf_dev_agent.context import default_db_path
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
    def _component_summary(comp: Any, include_source: bool = False) -> dict[str, Any]:
        """Pack a ComponentRow into a compact dict for tool output."""
        out = {
            "id": comp.id,
            "component_type": comp.component_type,
            "api_name": comp.api_name,
            "parent_id": comp.parent_id,
            "metadata": comp.metadata,
            "last_indexed_at": comp.last_indexed_at,
        }
        if include_source and comp.source is not None:
            out["source"] = comp.source
        return out

    def _exec_code_search(
        self,
        query: str,
        component_type: str | None = None,
        include_source: bool = False,
        limit: int = 25,
    ) -> dict[str, Any]:
        """Substring search across the metadata index."""
        from sf_dev_agent.context import MetadataIndex

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
                "results": [self._component_summary(h, include_source) for h in hits],
            }

    def _exec_sf_dependency_graph(
        self,
        component_id: str | None = None,
        component_type: str | None = None,
        api_name: str | None = None,
        direction: str = "both",
    ) -> dict[str, Any]:
        """Return relationship edges for one component."""
        from sf_dev_agent.context import MetadataIndex

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

    def _exec_build_metadata_index(
        self,
        component_types: list[str] | None = None,
        full_refresh: bool = False,
    ) -> dict[str, Any]:
        """Refresh the SQLite metadata index from the connected org."""
        from sf_dev_agent.context import build_index

        db_path = self._resolve_index_db_path()
        result = build_index(
            org_alias=self.org.org_alias,
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

    def _exec_embed_metadata_index(
        self,
        component_types: list[str] | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Populate/refresh embeddings for indexed components."""
        from sf_dev_agent.context import MetadataIndex, create_embedder

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

    # ------------------------------------------------------------------
    # Knowledge-base-backed tools
    # ------------------------------------------------------------------

    def _exec_embed_knowledge_base(
        self,
        category: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Auto-load bundled entries (if needed) and refresh embeddings."""
        from sf_dev_agent.context import KnowledgeBase, create_embedder

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

    def _exec_knowledge_search(
        self,
        query: str,
        category: str | None = None,
        limit: int = 5,
        min_score: float = 0.0,
    ) -> dict[str, Any]:
        """Embed the query and rank knowledge entries by cosine similarity."""
        from sf_dev_agent.context import KnowledgeBase, create_embedder

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

    def _exec_semantic_search(
        self,
        query: str,
        component_type: str | None = None,
        limit: int = 10,
        min_score: float = 0.0,
    ) -> dict[str, Any]:
        """Embed the query and rank components by cosine similarity."""
        from sf_dev_agent.context import MetadataIndex, create_embedder

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
