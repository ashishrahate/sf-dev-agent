"""Core agent loop — ReAct pattern with plan → approve → execute phases."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt

from sf_dev_agent.memory import (
    ConversationLog,
    MemoryScope,
    WorkingMemoryStore,
)
from sf_dev_agent.models.schemas import (
    ExecutionPlan,
    OrgConnection,
    PlanStep,
    PreflightCheck,
    RiskLevel,
    Task,
    TaskStatus,
)
from sf_dev_agent.prompts import load_system_prompt
from sf_dev_agent.providers.base import LLMProvider
from sf_dev_agent.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)
console = Console()

# Tools that never require approval (read-only)
READ_ONLY_TOOLS = frozenset({
    "sf_metadata_describe",
    "sf_soql_query",
    "sf_retrieve",
    "sf_dependency_graph",
    "code_search",
    "code_lint",
    "knowledge_search",
    "memory_recall",
    "file_read",
    "submit_plan",
})

# Tools that always require plan approval before execution
WRITE_TOOLS = frozenset({
    "file_write",
    "file_delete",
    "sf_source_deploy",
    "sf_source_delete",
    "sf_apex_execute",
    "sf_test_run",
    "sf_data_operation",
    "bash",
})


class AgentLoop:
    """The core agent loop implementing plan → approve → execute."""

    def __init__(
        self,
        org: OrgConnection,
        provider: LLMProvider,
        max_iterations: int = 50,
        mock_org: bool = False,
        working_memory: WorkingMemoryStore | None = None,
    ) -> None:
        self.org = org
        self.provider = provider
        self.max_iterations = max_iterations
        self.tool_registry = ToolRegistry(org=org, mock_org=mock_org)
        self.working_memory = working_memory
        # Initialized for real in run() once the task_id is known. Until
        # then we use a placeholder ConversationLog with no store so the
        # type stays consistent and providers can still iterate it.
        self.conversation: ConversationLog = ConversationLog(task_id="")
        self.current_task: Task | None = None
        self.plan_approved = False

        self.system_prompt = load_system_prompt(
            TENANT_ID=org.tenant_id,
            ORG_ALIAS=org.org_alias,
            ORG_TYPE=org.org_type,
            INSTANCE_URL=org.instance_url,
            API_VERSION=org.api_version,
            AGENT_MODEL=provider.model_name,
            TIMESTAMP=datetime.now(UTC).isoformat(),
        )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def run(self, user_request: str) -> Task:
        """Execute a full task: plan → approve → execute."""
        task_id = f"task_{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
        self.current_task = Task(
            task_id=task_id,
            tenant_id=self.org.tenant_id,
            user_request=user_request,
        )

        # Persist the task row up-front and bind the conversation log to
        # this task_id. Persistence failures are best-effort — log and
        # continue on a plain in-memory log so the agent can still run
        # without working memory.
        scope = MemoryScope(tenant_id=self.org.tenant_id, org_alias=self.org.org_alias)
        if self.working_memory is not None:
            try:
                self.working_memory.create_task(
                    task_id=task_id,
                    scope=scope,
                    user_request=user_request,
                    status=TaskStatus.PLANNING.value,
                )
                self.conversation = ConversationLog(
                    task_id=task_id, store=self.working_memory,
                )
            except Exception:
                logger.exception(
                    "Failed to create persistent task row; continuing without persistence"
                )
                self.conversation = ConversationLog(task_id=task_id)
        else:
            self.conversation = ConversationLog(task_id=task_id)

        self._transition(TaskStatus.PLANNING)

        self.conversation.append({"role": "user", "content": user_request})

        console.print(Panel(user_request, title="[bold]Task Request", border_style="blue"))

        # Phase 1: Planning loop (read-only tools allowed)
        console.print("\n[bold cyan]Phase 1: Planning[/bold cyan]")
        self._agent_loop(phase="planning")

        # Present plan and ask for approval
        if self.current_task.plan:
            self._present_plan(self.current_task.plan)
            approved = self._request_approval()

            if not approved:
                self._transition(TaskStatus.FAILED)
                self._persist_terminal_result(success=False, summary="plan rejected")
                console.print("[bold red]Plan rejected by user. Task cancelled.[/bold red]")
                return self.current_task

            self.plan_approved = True
            if self.working_memory is not None and self.current_task is not None:
                try:
                    self.working_memory.set_plan_approved(
                        self.current_task.task_id, approved=True,
                    )
                except Exception:
                    logger.exception(
                        "Failed to persist plan-approval flag for task %s",
                        self.current_task.task_id,
                    )
            self._transition(TaskStatus.EXECUTING)

            console.print("\n[bold cyan]Phase 2: Executing approved plan[/bold cyan]")
            self.conversation.append({
                "role": "user",
                "content": "Plan approved. Proceed with execution.",
            })
            self._agent_loop(phase="execution")
            self._transition(TaskStatus.COMPLETE)
            self._persist_terminal_result(success=True, summary="completed")
        else:
            console.print(
                "[bold yellow]Agent did not produce a structured plan. "
                "Task may have been answered directly.[/bold yellow]"
            )
            self._transition(TaskStatus.COMPLETE)
            self._persist_terminal_result(success=True, summary="answered without plan")

        return self.current_task

    def _persist_terminal_result(self, success: bool, summary: str) -> None:
        """Stamp the final result_json on the persistent task row."""
        if self.working_memory is None or self.current_task is None:
            return
        try:
            self.working_memory.set_result(
                task_id=self.current_task.task_id,
                result_json=json.dumps({"success": success, "summary": summary}),
                status=self.current_task.status.value,
            )
        except Exception:
            logger.exception(
                "Failed to persist terminal result for task %s",
                self.current_task.task_id,
            )

    # ------------------------------------------------------------------
    # Agent loop (ReAct)
    # ------------------------------------------------------------------

    def _agent_loop(self, phase: str) -> None:
        """Run the ReAct loop: call LLM, process tool calls, repeat."""
        for iteration in range(self.max_iterations):
            logger.info("Agent loop iteration %d (phase=%s)", iteration + 1, phase)

            response = self.provider.chat(
                system=self.system_prompt,
                messages=self.conversation.as_messages(),
                tools=self.tool_registry.get_tool_definitions(),
            )

            # Rebuild assistant content blocks in internal format
            assistant_content: list[dict[str, Any]] = []
            tool_calls: list[dict[str, Any]] = []

            for text in response.text_blocks:
                assistant_content.append({"type": "text", "text": text})
                self._display_text(text)

            for tc in response.tool_calls:
                assistant_content.append({
                    "type": "tool_use",
                    "id": tc.id,
                    "name": tc.name,
                    "input": tc.input,
                })
                tool_calls.append({"id": tc.id, "name": tc.name, "input": tc.input})

            self.conversation.append({"role": "assistant", "content": assistant_content})

            if not tool_calls:
                logger.info("Agent completed %s phase (no more tool calls)", phase)
                break

            tool_results = [
                self._execute_tool(call["name"], call["input"], call["id"], phase)
                for call in tool_calls
            ]
            self.conversation.append({"role": "user", "content": tool_results})

            if response.stop_reason == "end_turn":
                logger.info("Agent signaled end_turn in %s phase", phase)
                break

        else:
            console.print(
                f"[bold red]Agent hit max iterations ({self.max_iterations}) "
                f"in {phase} phase.[/bold red]"
            )

    # ------------------------------------------------------------------
    # Tool execution with gating
    # ------------------------------------------------------------------

    def _execute_tool(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_use_id: str,
        phase: str,
    ) -> dict[str, Any]:
        """Execute a tool, enforcing phase-based write gating."""
        console.print(
            f"  [dim]Tool call:[/dim] [bold]{tool_name}[/bold] "
            f"[dim]{json.dumps(tool_input, indent=None)[:200]}[/dim]"
        )

        # submit_plan is intercepted here — never reaches the registry executor.
        if tool_name == "submit_plan":
            return self._handle_submit_plan(tool_input, tool_use_id)

        if tool_name in WRITE_TOOLS and phase == "planning":
            msg = (
                f"Tool '{tool_name}' is a write operation and cannot execute "
                "during planning. Include it as a step in the execution plan."
            )
            console.print(f"  [bold red]BLOCKED:[/bold red] {msg}")
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": f"ERROR: {msg}",
                "is_error": True,
            }

        if tool_name in WRITE_TOOLS and not self.plan_approved:
            msg = f"Tool '{tool_name}' requires an approved plan before execution."
            console.print(f"  [bold red]BLOCKED:[/bold red] {msg}")
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": f"ERROR: {msg}",
                "is_error": True,
            }

        try:
            result = self.tool_registry.execute(tool_name, tool_input)
            result_str = json.dumps(result) if isinstance(result, dict) else str(result)
            console.print(f"  [green]OK[/green] [dim]({len(result_str)} chars)[/dim]")
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": result_str,
            }
        except Exception as e:
            error_msg = f"{type(e).__name__}: {e}"
            console.print(f"  [bold red]ERROR:[/bold red] {error_msg}")
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": f"ERROR: {error_msg}",
                "is_error": True,
            }

    # ------------------------------------------------------------------
    # Plan presentation and approval
    # ------------------------------------------------------------------

    def _handle_submit_plan(
        self, tool_input: dict[str, Any], tool_use_id: str
    ) -> dict[str, Any]:
        """Parse the LLM's submit_plan call into an ExecutionPlan and store it."""
        try:
            plan = self._parse_plan(tool_input)
            if self.current_task:
                self.current_task.plan = plan
                if self.working_memory is not None:
                    try:
                        self.working_memory.set_plan(
                            task_id=self.current_task.task_id,
                            plan_json=json.dumps(tool_input, default=str),
                        )
                    except Exception:
                        logger.exception(
                            "Failed to persist plan for task %s",
                            self.current_task.task_id,
                        )
            console.print("  [green]Plan registered[/green] — awaiting user approval")
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": json.dumps({
                    "status": "plan_registered",
                    "steps": len(plan.steps),
                    "risk": plan.risk_assessment.value,
                }),
            }
        except Exception as exc:
            error_msg = f"submit_plan failed to parse: {exc}"
            console.print(f"  [bold red]ERROR:[/bold red] {error_msg}")
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": f"ERROR: {error_msg}",
                "is_error": True,
            }

    def _parse_plan(self, data: dict[str, Any]) -> ExecutionPlan:
        """Coerce LLM output into a validated ExecutionPlan."""
        def _risk(val: Any) -> str:
            return str(val).lower() if val else "low"

        steps = [
            PlanStep(
                step_number=s.get("step_number", i + 1),
                action=s.get("action", ""),
                target=s.get("target", ""),
                mode=str(s.get("mode", "create")).lower(),
                risk=RiskLevel(_risk(s.get("risk", "low"))),
                description=s.get("description", ""),
            )
            for i, s in enumerate(data.get("steps", []))
        ]

        checks = [
            PreflightCheck(label=c.get("label", ""), result=c.get("result", ""))
            for c in data.get("preflight_checks", [])
        ]

        return ExecutionPlan(
            summary=data.get("summary", ""),
            preflight_checks=checks,
            steps=steps,
            risk_assessment=RiskLevel(_risk(data.get("risk_assessment", "low"))),
            risk_reasoning=data.get("risk_reasoning", ""),
            rollback_strategy=data.get("rollback_strategy", ""),
            components_created=int(data.get("components_created", 0)),
            components_modified=int(data.get("components_modified", 0)),
            components_deleted=int(data.get("components_deleted", 0)),
            test_classes_affected=int(data.get("test_classes_affected", 0)),
        )

    def _present_plan(self, plan: ExecutionPlan) -> None:
        """Render the execution plan for the user."""
        md = f"## Plan\n\n**{plan.summary}**\n\n"

        if plan.preflight_checks:
            md += "### Pre-flight Checks\n"
            for check in plan.preflight_checks:
                md += f"- {check.label}: {check.result}\n"
            md += "\n"

        md += "### Steps\n"
        for step in plan.steps:
            md += (
                f"{step.step_number}. **{step.action}** -> `{step.target}` "
                f"({step.mode}) — Risk: {step.risk.value}\n"
                f"   {step.description}\n"
            )

        md += f"\n### Risk Assessment\n**{plan.risk_assessment.value.upper()}** — {plan.risk_reasoning}\n"
        md += f"\n### Rollback Strategy\n{plan.rollback_strategy}\n"
        md += (
            f"\n### Impact\n"
            f"- Components created: {plan.components_created}\n"
            f"- Components modified: {plan.components_modified}\n"
            f"- Components deleted: {plan.components_deleted}\n"
            f"- Test classes affected: {plan.test_classes_affected}\n"
        )

        console.print(Panel(Markdown(md), title="[bold]Execution Plan", border_style="yellow"))

    def _request_approval(self) -> bool:
        """Ask the user to approve, reject, or modify the plan."""
        console.print()
        choice = Prompt.ask(
            "[bold yellow]Approve this plan?[/bold yellow]",
            choices=["yes", "no", "modify"],
            default="no",
        )

        if choice == "yes":
            return True
        elif choice == "modify":
            feedback = Prompt.ask("[bold]What would you like to change?[/bold]")
            self.conversation.append({
                "role": "user",
                "content": f"Please revise the plan: {feedback}",
            })
            self._agent_loop(phase="planning")
            if self.current_task and self.current_task.plan:
                self._present_plan(self.current_task.plan)
                return self._request_approval()
            return False
        else:
            return False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _transition(self, new_status: TaskStatus) -> None:
        if self.current_task:
            old = self.current_task.status
            self.current_task.status = new_status
            self.current_task.updated_at = datetime.now(UTC)
            logger.info(
                "Task %s: %s → %s",
                self.current_task.task_id,
                old.value,
                new_status.value,
            )
            console.print(f"[dim]State: {old.value} -> {new_status.value}[/dim]")
            # Mirror the transition into working memory if persistence is on.
            if self.working_memory is not None:
                try:
                    self.working_memory.update_task_status(
                        task_id=self.current_task.task_id,
                        status=new_status.value,
                    )
                except Exception:
                    logger.exception(
                        "Failed to persist status transition for task %s",
                        self.current_task.task_id,
                    )

    def _display_text(self, text: str) -> None:
        console.print(Markdown(text))
