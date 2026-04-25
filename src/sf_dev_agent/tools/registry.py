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
})


class ToolRegistry:
    """Manages tool schemas and executors for the agent."""

    def __init__(self, org: OrgConnection, mock_org: bool = False) -> None:
        self.org = org
        self.mock_org = mock_org
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
