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

from sf_dev_agent.index_freshness import check_freshness, format_freshness_line
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
from sf_dev_agent.providers.base import LLMProvider, consume_stream
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

# Statuses that mean a task is finished and resume() should short-circuit.
_TERMINAL_TASK_STATUSES: frozenset[TaskStatus] = frozenset({
    TaskStatus.COMPLETE,
    TaskStatus.FAILED,
    TaskStatus.ROLLED_BACK,
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
        streaming: bool = False,
    ) -> None:
        self.org = org
        self.provider = provider
        self.max_iterations = max_iterations
        self.tool_registry = ToolRegistry(org=org, mock_org=mock_org)
        self.working_memory = working_memory
        # Streaming = render assistant text deltas live as they arrive
        # from the provider. The REPL turns this on; one-shot CLI keeps
        # the buffered Markdown render. Both go through chat_stream
        # under the hood; the flag only changes presentation.
        self.streaming = streaming
        # Initialized for real in run() once the task_id is known. Until
        # then we use a placeholder ConversationLog with no store so the
        # type stays consistent and providers can still iterate it.
        self.conversation: ConversationLog = ConversationLog(task_id="")
        self.current_task: Task | None = None
        self.plan_approved = False

        # Compute index-freshness once at construction. The REPL can refresh
        # the prompt later via /index or by recreating the AgentLoop.
        try:
            from sf_dev_agent.context import default_db_path
            freshness = check_freshness(default_db_path(), org.org_alias)
            freshness_line = format_freshness_line(freshness)
        except Exception:
            logger.exception("Could not compute index freshness; using fallback")
            freshness_line = "unknown (freshness check failed)"

        self.system_prompt = load_system_prompt(
            TENANT_ID=org.tenant_id,
            ORG_ALIAS=org.org_alias,
            ORG_TYPE=org.org_type,
            INSTANCE_URL=org.instance_url,
            API_VERSION=org.api_version,
            AGENT_MODEL=provider.model_name,
            TIMESTAMP=datetime.now(UTC).isoformat(),
            INDEX_FRESHNESS=freshness_line,
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

        return self._run_planning_then_execution()

    @classmethod
    def resume(
        cls,
        task_id: str,
        org: OrgConnection,
        provider: LLMProvider,
        working_memory: WorkingMemoryStore,
        max_iterations: int = 50,
        mock_org: bool = False,
    ) -> Task:
        """Pick up a persisted task and continue from its last status.

        Loads the task row + conversation transcript, reconstructs the
        AgentLoop state, and dispatches based on `TaskStatus`:

            received / planning (no plan saved)  -> redo Phase 1, then Phase 2
            awaiting_approval / planning + plan  -> redisplay plan, prompt
            executing                            -> Phase 2 only (skip planning)
            terminal (complete/failed/...)       -> short-circuit, return Task

        A task with `plan_json` saved but `status=planning` is treated as
        `awaiting_approval` — that's the most likely shape after a crash
        between `submit_plan` and the approval prompt.

        Raises:
            ValueError if the task isn't found or belongs to a different
            tenant than the supplied `org`.
        """
        row = working_memory.get_task(task_id)
        if row is None:
            raise ValueError(f"task {task_id!r} not found in working memory")
        if row.tenant_id != org.tenant_id:
            raise ValueError(
                f"task {task_id!r} belongs to tenant {row.tenant_id!r}, "
                f"not the current org's tenant {org.tenant_id!r}"
            )

        self = cls(
            org=org, provider=provider,
            max_iterations=max_iterations, mock_org=mock_org,
            working_memory=working_memory,
        )

        # Reconstruct Task model + plan from persisted state.
        self.current_task = Task(
            task_id=row.id,
            tenant_id=row.tenant_id,
            status=TaskStatus(row.status),
            user_request=row.user_request,
        )
        if row.plan_json:
            try:
                self.current_task.plan = self._parse_plan(json.loads(row.plan_json))
            except Exception:
                logger.exception(
                    "Failed to rehydrate plan_json for task %s; treating as no plan",
                    task_id,
                )
        self.plan_approved = row.plan_approved

        # Seed the conversation from disk. ConversationLog.seed= pre-fills
        # the in-memory list WITHOUT re-persisting (those rows are already
        # there); subsequent appends go to disk normally.
        seeded = working_memory.load_messages(task_id)
        self.conversation = ConversationLog(
            task_id=task_id, store=working_memory, seed=seeded,
        )

        console.print(Panel(
            f"task_id: {task_id}\nstatus: {row.status}\n"
            f"messages: {len(seeded)} | plan_approved: {row.plan_approved}",
            title="[bold]Resuming task", border_style="cyan",
        ))

        status = TaskStatus(row.status)

        # Terminal — nothing to do; print summary for the operator.
        if status in _TERMINAL_TASK_STATUSES:
            console.print(
                f"[dim]Task is already in terminal state: {status.value}.[/dim]"
            )
            return self.current_task

        # Already executing — skip Phase 1 entirely.
        if status == TaskStatus.EXECUTING:
            return self._run_execution_only(append_transition_message=False)

        # Awaiting approval, OR planning with a plan saved (crash window).
        if status == TaskStatus.AWAITING_APPROVAL or (
            status == TaskStatus.PLANNING and self.current_task.plan is not None
        ):
            if self.current_task.plan is None:
                # Defensive: AWAITING_APPROVAL implies a plan exists.
                raise ValueError(
                    f"task {task_id!r} is awaiting approval but has no saved plan"
                )
            return self._run_approval_then_execution()

        # Default: planning loop — picks up wherever the prior session
        # stopped (the conversation transcript is the agent's memory).
        return self._run_planning_then_execution()

    # ------------------------------------------------------------------
    # Phase composition — extracted from run() so resume() can pick up
    # at any phase without duplicating logic.
    # ------------------------------------------------------------------

    def _run_planning_then_execution(self) -> Task:
        """Phase 1 (planning) → Phase 2 (gated execution)."""
        console.print("\n[bold cyan]Phase 1: Planning[/bold cyan]")
        self._agent_loop(phase="planning")
        return self._run_approval_then_execution()

    def _run_approval_then_execution(self) -> Task:
        """Post-Phase-1: present plan, request approval, run Phase 2 if approved.

        If the agent didn't produce a structured plan, treat the run as
        complete (it answered the question directly).
        """
        if self.current_task is None:
            raise RuntimeError("AgentLoop._run_approval_then_execution called with no current_task")

        if not self.current_task.plan:
            console.print(
                "[bold yellow]Agent did not produce a structured plan. "
                "Task may have been answered directly.[/bold yellow]"
            )
            self._transition(TaskStatus.COMPLETE)
            self._persist_terminal_result(success=True, summary="answered without plan")
            return self.current_task

        # Persisted state: planning → awaiting_approval, so a crash here
        # leaves a clear "redisplay plan and re-prompt" signal for resume.
        self._transition(TaskStatus.AWAITING_APPROVAL)
        self._present_plan(self.current_task.plan)
        approved = self._request_approval()

        if not approved:
            self._transition(TaskStatus.FAILED)
            self._persist_terminal_result(success=False, summary="plan rejected")
            console.print("[bold red]Plan rejected by user. Task cancelled.[/bold red]")
            return self.current_task

        self.plan_approved = True
        if self.working_memory is not None:
            try:
                self.working_memory.set_plan_approved(
                    self.current_task.task_id, approved=True,
                )
            except Exception:
                logger.exception(
                    "Failed to persist plan-approval flag for task %s",
                    self.current_task.task_id,
                )

        return self._run_execution_only(append_transition_message=True)

    def _run_execution_only(self, *, append_transition_message: bool) -> Task:
        """Phase 2. Assumes a plan is approved and the conversation has the
        transition message already (resume from EXECUTING) or that we should
        add it here (fresh approval flow).
        """
        if self.current_task is None:
            raise RuntimeError("AgentLoop._run_execution_only called with no current_task")

        self._transition(TaskStatus.EXECUTING)

        if append_transition_message:
            self.conversation.append({
                "role": "user",
                "content": "Plan approved. Proceed with execution.",
            })

        console.print("\n[bold cyan]Phase 2: Executing approved plan[/bold cyan]")
        self._agent_loop(phase="execution")
        self._transition(TaskStatus.COMPLETE)
        self._persist_terminal_result(success=True, summary="completed")
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
        """Run the ReAct loop: call LLM, process tool calls, repeat.

        Always goes through `chat_stream` under the hood. With
        `self.streaming=True` (REPL), text deltas render live as they
        arrive. With `self.streaming=False` (one-shot CLI), deltas are
        buffered and rendered as Markdown at end-of-message — same UX
        as the pre-streaming code path.
        """
        for iteration in range(self.max_iterations):
            logger.info("Agent loop iteration %d (phase=%s)", iteration + 1, phase)

            chunks = self.provider.chat_stream(
                system=self.system_prompt,
                messages=self.conversation.as_messages(),
                tools=self.tool_registry.get_tool_definitions(),
            )

            if self.streaming:
                # Print each delta to the live terminal as it arrives.
                # Markdown formatting can't be applied incrementally, so
                # use raw print here; the user sees tokens stream by.
                response = consume_stream(
                    chunks,
                    on_text=lambda t: console.print(t, end="", soft_wrap=True),
                )
                if response.text_blocks:
                    # Terminate the streaming line cleanly.
                    console.print()
            else:
                response = consume_stream(chunks)
                for text in response.text_blocks:
                    self._display_text(text)

            # Rebuild assistant content blocks in internal format.
            assistant_content: list[dict[str, Any]] = []
            tool_calls: list[dict[str, Any]] = []

            for text in response.text_blocks:
                assistant_content.append({"type": "text", "text": text})

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
            # Persisted state goes back to planning while the agent revises;
            # _present_plan + _request_approval below will flip it to
            # awaiting_approval again.
            self._transition(TaskStatus.PLANNING)
            self._agent_loop(phase="planning")
            if self.current_task and self.current_task.plan:
                self._transition(TaskStatus.AWAITING_APPROVAL)
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
