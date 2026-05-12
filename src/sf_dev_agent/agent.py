"""Core agent loop — ReAct pattern with plan → approve → execute phases."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt

from sf_dev_agent.index_freshness import check_freshness, format_freshness_line
from sf_dev_agent.interrupt import InterruptListener
from sf_dev_agent.memory import (
    ConversationLog,
    MemoryScope,
    WorkingMemoryStore,
)
from sf_dev_agent.models.schemas import (
    AgentMode,
    ExecutionPlan,
    OrgConnection,
    PlanStep,
    PreflightCheck,
    RiskLevel,
    Task,
    TaskStatus,
)
from sf_dev_agent.audit import LLMAuditStore, LLMInvocationRecord
from sf_dev_agent.prompts import load_system_prompt
from sf_dev_agent.providers.base import LLMProvider, consume_stream
from sf_dev_agent.repl_ui import (
    render_stream_terminator,
    render_streaming_text,
    render_file_write_diff,
    render_reindex_summary,
    render_tool_blocked,
    render_tool_call_header,
    render_tool_error,
    render_tool_ok,
    tool_status,
)
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
    "list_resumable_tasks",
    "get_task_summary",
    "request_resume",
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


class BusyError(RuntimeError):
    """Raised by AgentLoop.prompt() when the agent is mid-task.

    Slice 1 of the PI-style input-routing refactor: the busy gate moves
    onto the agent itself. Until slice 3 lands the steer / follow-up
    queues, the REPL surfaces this as a "task X is in flight" message
    instead of starting a fresh run. Slice 3 will catch this and route
    the input into a queue.
    """
    def __init__(self, text: str, *, active_task_id: str | None = None) -> None:
        super().__init__(
            f"agent busy on task {active_task_id!r}; "
            f"refusing new prompt {text[:40]!r}"
        )
        self.text = text
        self.active_task_id = active_task_id


# Bare approval tokens — short input that the user almost certainly meant
# as a yes/no/modify reply to a prior approval prompt, not a new task.
# Slice 2's safety net checks this when no plan emerges from planning.
_STRAY_APPROVAL_PATTERN = re.compile(
    r"^\s*(y|yes|n|no|modify)\s*$", re.IGNORECASE,
)


def _looks_like_stray_approval(text: str) -> bool:
    """Heuristic: did the user accidentally answer a hung approval as a new task?"""
    if not text:
        return False
    return bool(_STRAY_APPROVAL_PATTERN.match(text))


def drive_approval_loop(agent: AgentLoop) -> Task | None:
    """Default approval-loop driver: prompt the user until status is terminal.

    Slice 2 — the I/O loop that used to be `_request_approval` lives
    here, at module scope. Both the persistent REPL and the one-shot
    `AgentLoop.run()` / `AgentLoop.resume()` paths call this after the
    agent yields in AWAITING_APPROVAL. Future slices (5) may have the
    REPL substitute its own driver to handle steer / follow-up routing.

    Returns the final `Task` after the loop terminates, or None when
    the agent has no current_task (defensive).
    """
    while (
        agent.current_task is not None
        and agent.current_task.status == TaskStatus.AWAITING_APPROVAL
    ):
        console.print()
        try:
            choice = Prompt.ask(
                "[bold yellow]Approve this plan?[/bold yellow]",
                choices=["yes", "no", "modify"],
                default="no",
            )
        except (EOFError, KeyboardInterrupt):
            # Leave the task in AWAITING_APPROVAL — Slice 1's busy gate
            # will catch it on the next REPL dispatch and surface a
            # /resume hint. No silent state-loss.
            logger.warning(
                "approval prompt aborted (EOF/interrupt); task left awaiting"
            )
            return agent.current_task

        if choice == "yes":
            return agent.approve_plan(True)
        if choice == "no":
            return agent.approve_plan(False)

        try:
            feedback = Prompt.ask("[bold]What would you like to change?[/bold]")
        except (EOFError, KeyboardInterrupt):
            logger.warning(
                "modify-feedback prompt aborted (EOF/interrupt); task left awaiting"
            )
            return agent.current_task

        agent.modify_plan(feedback)
        # Loop: if modify produced a new plan, we're back in
        # AWAITING_APPROVAL and the next iteration prompts again. If
        # not, status is terminal and the while exits.

    return agent.current_task


# Per-mode guidance injected into the system prompt. The plan-mode block is
# empty so the existing prompt body (Operating Modes / Phase 1 / Phase 2 /
# Plan Format sections) speaks for itself — backwards-compatible default.
# Execution and general modes get a loud override at the top so the LLM
# knows to skip submit_plan and adapt its behavior.
_MODE_INSTRUCTIONS: dict[AgentMode, str] = {
    AgentMode.PLAN: "",
    AgentMode.EXECUTION: (
        "**MODE OVERRIDE — EXECUTION MODE.** The user has switched into "
        "execution mode. Do NOT call `submit_plan`. Skip the planning "
        "ceremony entirely. Execute the user's request directly using "
        "whichever tools are appropriate. The user has explicitly "
        "authorized writes without per-step approval for this session. "
        "The 'Operating Modes / Phase 1 / Phase 2 / Plan Format' "
        "guidance below DOES NOT APPLY in this mode — only the "
        "universal Salesforce Platform Expertise rules above do. Be "
        "concise; act decisively."
    ),
    AgentMode.GENERAL: (
        "**MODE OVERRIDE — GENERAL MODE.** The user has switched into "
        "general (read-only-default) mode. Do NOT call `submit_plan`. "
        "Default to read-only tools. If a write tool is genuinely "
        "required, call it directly — the user will be prompted to "
        "approve each call inline before it runs (the prompt is "
        "outside your view). Make tool inputs as small and scoped as "
        "possible since the user reviews each one. The 'Operating "
        "Modes / Phase 1 / Phase 2 / Plan Format' guidance below DOES "
        "NOT APPLY in this mode — only the universal Salesforce "
        "Platform Expertise rules above do. Prefer answering directly "
        "from retrieved context when the user is asking a question."
    ),
}


def _mode_instructions(mode: AgentMode) -> str:
    """Return the system-prompt override block for a given mode.

    Defensive: unknown mode returns the plan-mode (empty) block rather
    than raising — preserves agent function if someone hand-rolls a
    new enum value without updating this map.
    """
    return _MODE_INSTRUCTIONS.get(mode, "")


def _reindex_files_after_write(
    file_paths: list[Path],
    *,
    mock_org: bool = False,
    embedder: Any | None = None,
) -> dict[str, int]:
    """Re-parse the given files into the local SQLite index after a
    successful `file_write` or `sf_source_deploy` tool call.

    Best-effort by design — every step is wrapped so a parser bug, a
    SQLite hiccup, or a missing API key can't break the agent's tool
    flow. Files for which no parser is registered (`.txt`, `.json`,
    `README.md`, etc.) are silently counted in `skipped`.

    Returns ``{"components": N, "relationships": N, "embedded": N,
    "skipped": N}``.

    Embedding semantics:
      - ``mock_org=True`` skips the embed step entirely (no API quota).
      - When ``embedder`` is None, we auto-construct via
        ``create_embedder()`` only if ``GOOGLE_API_KEY`` is set —
        otherwise we skip rather than silently fall back to MockEmbedder
        (whose 64-d vectors would corrupt the real-Gemini 3072-d store).
      - Tests inject ``embedder=MockEmbedder()`` directly to exercise
        the embed branch without a real API call.
      - Hash-gating in ``embed_components`` means an unchanged file's
        re-write costs zero embedder calls.
    """
    import os
    from pathlib import Path as _Path

    summary: dict[str, int] = {
        "components": 0, "relationships": 0,
        "embedded": 0, "skipped": 0,
    }
    paths = [p for p in file_paths if isinstance(p, _Path)]
    if not paths:
        return summary

    try:
        from sf_dev_agent.context import MetadataIndex, default_db_path
        from sf_dev_agent.context.parsers.base import dispatch
    except Exception:
        logger.exception("Reindex post-write: imports failed")
        return summary

    upserted_ids: list[str] = []

    try:
        with MetadataIndex(default_db_path()) as index:
            for path in paths:
                try:
                    if not path.exists():
                        summary["skipped"] += 1
                        continue
                    parser = dispatch(path)
                    if parser is None:
                        summary["skipped"] += 1
                        continue
                    result = parser.parse(path)
                    n_c, n_r = index.reindex_from_parse_result(result)
                    summary["components"] += n_c
                    summary["relationships"] += n_r
                    upserted_ids.extend(c.id for c in result.components)
                except Exception:
                    logger.exception("Reindex failed for %s", path)
                    summary["skipped"] += 1

            # Embed step — gated, in-band, hash-aware. Skip cleanly when
            # mock_org, when no embedder is available, or when something
            # below raises. The index changes from above are already
            # committed by the time we get here.
            if upserted_ids and not mock_org:
                resolved_embedder = embedder
                if resolved_embedder is None:
                    if not os.environ.get("GOOGLE_API_KEY"):
                        logger.info(
                            "Auto-embed after write skipped: GOOGLE_API_KEY "
                            "not set (index updated; run /index later for embeddings)"
                        )
                    else:
                        try:
                            from sf_dev_agent.context import create_embedder
                            resolved_embedder = create_embedder()
                        except Exception:
                            logger.exception(
                                "Auto-embed after write: embedder construction failed"
                            )
                if resolved_embedder is not None:
                    try:
                        embed_result = index.embed_components(
                            embedder=resolved_embedder,
                            component_ids=upserted_ids,
                        )
                        summary["embedded"] = embed_result.embedded
                    except Exception:
                        logger.exception(
                            "Auto-embed after write failed; index still updated"
                        )
    except Exception:
        logger.exception("Reindex post-write failed at index level")

    return summary


def _capture_file_write_before(tool_input: dict[str, Any]) -> str | None:
    """Read existing file content for diffing before file_write overwrites.

    Returns "" if the target doesn't exist (new file → diff renders as
    all additions). Returns None on any error or path-traversal attempt
    so the diff is skipped silently — the executor's own validation
    still has the final word on whether the write happens.
    """
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return None
    try:
        from sf_dev_agent.paths import agent_workspace
        workspace = agent_workspace().resolve()
        target = (workspace / file_path).resolve()
        if not str(target).startswith(str(workspace)):
            return None
        if not target.exists():
            return ""
        return target.read_text(encoding="utf-8")
    except Exception:
        logger.exception("Could not capture file_write before-content")
        return None


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
        mode: AgentMode = AgentMode.PLAN,
        write_allowlist: set[str] | None = None,
    ) -> None:
        self.org = org
        self.provider = provider
        self.max_iterations = max_iterations
        self.tool_registry = ToolRegistry(
            org=org, mock_org=mock_org, working_memory=working_memory,
        )
        self.working_memory = working_memory
        # Streaming = render assistant text deltas live as they arrive
        # from the provider. The REPL turns this on; one-shot CLI keeps
        # the buffered Markdown render. Both go through chat_stream
        # under the hood; the flag only changes presentation.
        self.streaming = streaming
        # Operating mode — fixed at construction so a mid-task switch
        # can't change the safety contract. The REPL applies new modes
        # to subsequent tasks only.
        self.mode = mode
        # Per-tool allowlist for general-mode "always" approvals. When
        # the caller passes a set in, modifications persist across the
        # AgentLoop's lifetime AND across other AgentLoops sharing the
        # same set (REPL passes session.write_allowlist for per-session
        # persistence; tests + one-shot CLI get a fresh per-loop set).
        self._write_allowlist: set[str] = (
            write_allowlist if write_allowlist is not None else set()
        )
        # Initialized for real in run() once the task_id is known. Until
        # then we use a placeholder ConversationLog with no store so the
        # type stays consistent and providers can still iterate it.
        self.conversation: ConversationLog = ConversationLog(task_id="")
        self.current_task: Task | None = None
        self.plan_approved = False
        # Set when the LLM calls `request_resume` — the REPL reads this
        # after run() returns and triggers AgentLoop.resume() on it. The
        # value is the requested task_id; None means "no resume signal".
        self.resume_requested: str | None = None

        # Token-usage audit (Item 2). Opens lazily on the first record
        # write — keeps construction cheap and lets tests skip audit
        # entirely by passing `audit_store=None` semantics implicitly.
        # `_turn_idx` resets per task in run(). `_last_tools_run` captures
        # the tool names that ran in the previous iteration so the next
        # LLM call's row carries `triggered_by_tool` for attribution.
        self._audit_store: LLMAuditStore | None = None
        self._turn_idx: int = 0
        self._last_tools_run: list[str] = []

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
            AGENT_MODE_INSTRUCTIONS=_mode_instructions(mode),
        )

    # ------------------------------------------------------------------
    # Busy gate (Slice 1)
    # ------------------------------------------------------------------

    @property
    def active_task_id(self) -> str | None:
        """ID of the in-flight task, or None when idle / on a terminal task.

        A long-lived AgentLoop keeps `current_task` set even after a run
        completes; this property is the "is there real work pending"
        signal the REPL routes on.
        """
        if self.current_task is None:
            return None
        if self.current_task.status in _TERMINAL_TASK_STATUSES:
            return None
        return self.current_task.task_id

    @property
    def is_busy(self) -> bool:
        """True iff there's a task in flight on this agent instance."""
        return self.active_task_id is not None

    def prompt(self, text: str) -> Task:
        """Single entry point for new prompts (Slice 1 surface).

        While idle, this is a thin wrapper over `run()`. While busy, it
        raises `BusyError` — the REPL surfaces a hint to the user.
        Later slices (3+) replace the raise with steer / follow-up queue
        routing so approval answers and clarifications flow into the
        active run instead of starting a new task.

        Also resets `resume_requested` so a stale flag from a prior
        run on this long-lived instance can't fire twice.
        """
        if self.is_busy:
            raise BusyError(text, active_task_id=self.active_task_id)
        self.resume_requested = None
        return self.run(text)

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
        # Reset per-task audit counters so a re-used AgentLoop instance
        # doesn't carry turn indexes / tool attribution across tasks.
        self._turn_idx = 0
        self._last_tools_run = []

        # Persist the task row up-front and bind the conversation log to
        # this task_id. Persistence failures are best-effort — log and
        # continue on a plain in-memory log so the agent can still run
        # without working memory.
        scope = MemoryScope(tenant_id=self.org.tenant_id, org_alias=self.org.org_alias)
        if self.working_memory is not None:
            try:
                # Slice C: persist the mode at task creation so resume()
                # can faithfully reconstruct it later. Initial status is
                # PLANNING for plan mode (Phase 1 is about to run) and
                # EXECUTING for non-plan modes (no Phase 1 happens).
                initial_status = (
                    TaskStatus.PLANNING.value
                    if self.mode == AgentMode.PLAN
                    else TaskStatus.EXECUTING.value
                )
                self.working_memory.create_task(
                    task_id=task_id,
                    scope=scope,
                    user_request=user_request,
                    status=initial_status,
                    mode=self.mode.value,
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

        # Initial transition reflects the mode. Plan mode goes through
        # PLANNING → AWAITING_APPROVAL → EXECUTING. Execution + general
        # skip planning and head straight to EXECUTING — keeps the
        # status machine honest about what's actually happening.
        if self.mode == AgentMode.PLAN:
            self._transition(TaskStatus.PLANNING)
        else:
            self._transition(TaskStatus.EXECUTING)

        self.conversation.append({"role": "user", "content": user_request})

        console.print(Panel(user_request, title="[bold]Task Request", border_style="blue"))

        if self.mode == AgentMode.PLAN:
            result = self._run_planning_then_execution()
        else:
            result = self._run_direct()

        # Slice 2: phase composition yields in AWAITING_APPROVAL instead
        # of blocking inside `_request_approval`. Drive the default
        # approval UX here so one-shot CLI / non-REPL callers see the
        # same end-to-end behavior as before. The REPL substitutes its
        # own driver in a later slice.
        if (
            self.current_task is not None
            and self.current_task.status == TaskStatus.AWAITING_APPROVAL
        ):
            driven = drive_approval_loop(self)
            return driven if driven is not None else result
        return result

    @classmethod
    def resume(
        cls,
        task_id: str,
        org: OrgConnection,
        provider: LLMProvider,
        working_memory: WorkingMemoryStore,
        max_iterations: int = 50,
        mock_org: bool = False,
        mode: AgentMode = AgentMode.PLAN,
        write_allowlist: set[str] | None = None,
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

        # Slice C: the persisted row's mode is authoritative. A task
        # started in plan mode shouldn't suddenly become execution on
        # resume just because the REPL session changed mode in the
        # meantime. Caller-supplied `mode` is treated as a fallback for
        # legacy rows that pre-date the column.
        try:
            persisted_mode = AgentMode(row.mode)
        except (ValueError, AttributeError):
            persisted_mode = mode
        self = cls(
            org=org, provider=provider,
            max_iterations=max_iterations, mock_org=mock_org,
            working_memory=working_memory,
            mode=persisted_mode, write_allowlist=write_allowlist,
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

        # Continue turn indexing where the prior run left off so resumed
        # rows in `llm_invocations` don't collide on (task_id, turn_idx).
        # Best-effort: a stale audit store doesn't block resume.
        try:
            store = self._get_audit_store()
            if store is not None:
                prior = store.list_for_task(task_id)
                if prior:
                    self._turn_idx = max(r.turn_idx for r in prior) + 1
        except Exception:
            logger.exception("Could not seed resume turn_idx from audit store")

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
            result = self._run_approval_then_execution()
            # Slice 2: drive the default approval UX so resume_cli /
            # one-shot resume sees the same behavior as before.
            if (
                self.current_task is not None
                and self.current_task.status == TaskStatus.AWAITING_APPROVAL
            ):
                driven = drive_approval_loop(self)
                return driven if driven is not None else result
            return result

        # Default: planning loop — picks up wherever the prior session
        # stopped (the conversation transcript is the agent's memory).
        result = self._run_planning_then_execution()
        if (
            self.current_task is not None
            and self.current_task.status == TaskStatus.AWAITING_APPROVAL
        ):
            driven = drive_approval_loop(self)
            return driven if driven is not None else result
        return result

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
        """Post-Phase-1: present plan and yield in AWAITING_APPROVAL.

        Slice 2 of the PI-style refactor: this method no longer blocks
        on a user prompt. It transitions to AWAITING_APPROVAL, presents
        the plan, persists, and returns. The caller (REPL or one-shot
        CLI) calls `drive_approval_loop(self)` to run the actual prompt
        and dispatch to `approve_plan` / `modify_plan`.

        If the agent didn't produce a structured plan, treat the run as
        complete (it answered the question directly) — unless the
        user_request looks like a stray approval token left over from a
        prior hung task, in which case we fail loudly with a hint.
        """
        if self.current_task is None:
            raise RuntimeError("AgentLoop._run_approval_then_execution called with no current_task")

        if not self.current_task.plan:
            if _looks_like_stray_approval(self.current_task.user_request):
                console.print(
                    "[bold yellow]Looks like an approval token with no "
                    "active task to approve.[/bold yellow] If you meant to "
                    "continue an earlier task, try "
                    "[cyan]/resume --latest[/cyan]."
                )
                self._transition(TaskStatus.FAILED)
                self._persist_terminal_result(
                    success=False,
                    summary="bare approval token with no active plan",
                )
                return self.current_task

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
        # Slice 2: yield. The caller drives the approval UX via
        # drive_approval_loop(self) → approve_plan() / modify_plan().
        return self.current_task

    def approve_plan(self, approved: bool) -> Task:
        """Caller-facing continuation: accept or reject the current plan.

        Slice 2 — the REPL (or one-shot driver) calls this after the
        user answers the approval prompt. Approval starts Phase 2;
        rejection marks the task FAILED. Either way, the returned Task
        is in a terminal status.
        """
        if self.current_task is None:
            raise RuntimeError("approve_plan called with no current_task")
        if self.current_task.status != TaskStatus.AWAITING_APPROVAL:
            raise RuntimeError(
                f"approve_plan called on task in status {self.current_task.status.value}; "
                "expected awaiting_approval"
            )

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

    def modify_plan(self, feedback: str) -> Task:
        """Caller-facing continuation: revise the current plan with feedback.

        Slice 2 — replaces the recursive `_request_approval` modify
        branch. Appends the revision message, re-runs the planning
        phase, and either yields again in AWAITING_APPROVAL (caller
        loops) or transitions to FAILED if no new plan emerged.
        """
        if self.current_task is None:
            raise RuntimeError("modify_plan called with no current_task")
        if self.current_task.status != TaskStatus.AWAITING_APPROVAL:
            raise RuntimeError(
                f"modify_plan called on task in status {self.current_task.status.value}; "
                "expected awaiting_approval"
            )

        self.conversation.append({
            "role": "user",
            "content": f"Please revise the plan: {feedback}",
        })
        # Persisted state goes back to planning while the agent revises;
        # the loop below flips it back to awaiting_approval once a new
        # plan lands. Clear the existing plan first so "did a new plan
        # actually emerge?" is unambiguous below — without this we'd
        # mistake the stale plan for a successful revision.
        self.current_task.plan = None
        self._transition(TaskStatus.PLANNING)
        self._agent_loop(phase="planning")

        if self.current_task is not None and self.current_task.plan:
            self._transition(TaskStatus.AWAITING_APPROVAL)
            self._present_plan(self.current_task.plan)
            return self.current_task

        # Revision produced no new plan — fail loudly rather than
        # leaving the task in a half-state (the original bug surface).
        self._transition(TaskStatus.FAILED)
        self._persist_terminal_result(success=False, summary="modify produced no plan")
        console.print(
            "[bold yellow]Modify produced no new plan. Task marked failed.[/bold yellow]"
        )
        return self.current_task if self.current_task is not None else Task(
            task_id="unknown", tenant_id=self.org.tenant_id, user_request="",
            status=TaskStatus.FAILED,
        )

    def _run_direct(self) -> Task:
        """Single-phase loop for execution + general modes.

        No plan ceremony; the agent runs straight against the user's
        request. Write gating is handled per-tool in `_execute_tool`:
        execution mode passes writes through unconditionally; general
        mode prompts the user inline before each write.
        """
        if self.current_task is None:
            raise RuntimeError("AgentLoop._run_direct called with no current_task")

        mode_label = self.mode.value
        console.print(
            f"\n[bold cyan]Running in {mode_label} mode[/bold cyan]"
        )
        self._agent_loop(phase="execution")
        # Don't override an interrupt-driven FAILED status with COMPLETE.
        if self.current_task.status not in _TERMINAL_TASK_STATUSES:
            self._transition(TaskStatus.COMPLETE)
            self._persist_terminal_result(success=True, summary="completed")
        return self.current_task

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

        if self.mode == AgentMode.PLAN:
            console.print(
                "\n[bold cyan]Phase 2: Executing approved plan[/bold cyan]"
            )
        else:
            console.print(
                f"\n[bold cyan]Resuming in {self.mode.value} mode[/bold cyan]"
            )
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
    # Token-usage audit (Item 2)
    # ------------------------------------------------------------------

    def _get_audit_store(self) -> LLMAuditStore | None:
        """Lazily open the audit store. Returns None on open failure so the
        agent loop continues working even if SQLite is unhappy."""
        if self._audit_store is not None:
            return self._audit_store
        try:
            from sf_dev_agent.context import default_db_path
            self._audit_store = LLMAuditStore(default_db_path())
        except Exception:
            logger.exception("LLMAuditStore open failed — audit disabled for this run")
            self._audit_store = None
        return self._audit_store

    def _record_llm_invocation(
        self,
        *,
        started_at: str,
        duration_ms: int,
        response_usage: Any,
        stop_reason: str,
        emitted_tools: list[str],
    ) -> None:
        """Best-effort audit write — never raise into the agent loop."""
        if self.current_task is None:
            return
        store = self._get_audit_store()
        if store is None:
            return
        try:
            from sf_dev_agent.providers.base import TokenUsage
            usage = response_usage if isinstance(response_usage, TokenUsage) else TokenUsage()
            store.record(LLMInvocationRecord(
                tenant_id=self.org.tenant_id,
                org_alias=self.org.org_alias,
                task_id=self.current_task.task_id,
                turn_idx=self._turn_idx,
                provider=self.provider.__class__.__name__,
                model=self.provider.model_name,
                usage=usage,
                triggered_by_tool=(
                    self._last_tools_run[0] if self._last_tools_run else None
                ),
                emitted_tools=list(emitted_tools),
                stop_reason=stop_reason,
                started_at=started_at,
                duration_ms=duration_ms,
                mode=self.mode.value,
            ))
        except Exception:
            logger.exception("LLM audit record failed (continuing)")

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

        ESC / Ctrl+C: an `InterruptListener` watches stdin in a
        background thread. The streaming on_text callback polls the
        flag; when set, it raises `InterruptedError` which we catch
        alongside `KeyboardInterrupt`. The current iteration is
        abandoned, a synthetic user message records the interruption
        for the model's next turn, and the loop exits.
        """
        with InterruptListener() as interrupt:
            for iteration in range(self.max_iterations):
                logger.info("Agent loop iteration %d (phase=%s)", iteration + 1, phase)

                # Capture wall-clock around the LLM call only — token
                # accounting and timing should reflect provider latency,
                # not anything we did after the response landed.
                call_started_at = datetime.now(UTC).isoformat()
                call_started_perf = datetime.now(UTC)

                try:
                    chunks = self.provider.chat_stream(
                        system=self.system_prompt,
                        messages=self.conversation.as_messages(),
                        tools=self._mode_filtered_tool_definitions(),
                    )

                    if self.streaming:
                        # Print each delta to the live terminal as it
                        # arrives. The on_text callback also acts as
                        # the interrupt poll-point: if ESC fired, raise
                        # to abort the stream cleanly.
                        def on_text(t: str) -> None:
                            if interrupt.is_set():
                                raise InterruptedError("ESC pressed")
                            render_streaming_text(t)

                        response = consume_stream(chunks, on_text=on_text)
                        if response.text_blocks:
                            # Terminate the streaming line cleanly.
                            render_stream_terminator()
                    else:
                        response = consume_stream(chunks)
                        for text in response.text_blocks:
                            self._display_text(text)
                except (InterruptedError, KeyboardInterrupt):
                    self._handle_interrupt(phase)
                    return

                duration_ms = int(
                    (datetime.now(UTC) - call_started_perf).total_seconds() * 1000
                )

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

                # Item 2 — persist this LLM call's token usage + provenance.
                # Done AFTER conversation append + content rebuild so any
                # exception in the audit path can't desync state.
                emitted_tool_names = [tc.name for tc in response.tool_calls]
                self._record_llm_invocation(
                    started_at=call_started_at,
                    duration_ms=duration_ms,
                    response_usage=response.usage,
                    stop_reason=response.stop_reason,
                    emitted_tools=emitted_tool_names,
                )
                self._turn_idx += 1
                # `_last_tools_run` informs the NEXT iteration's
                # `triggered_by_tool` attribution. Updated below once
                # tools have actually run (failure to dispatch shouldn't
                # claim a tool was the trigger of the following turn).

                if not tool_calls:
                    logger.info("Agent completed %s phase (no more tool calls)", phase)
                    break

                # Check between LLM stream and tool dispatch — if the
                # user pressed ESC during the LLM response, don't fire
                # off the tools they tried to cancel.
                if interrupt.is_set():
                    self._handle_interrupt(phase)
                    return

                # Wrap tool dispatch in the same interrupt handler — a
                # KeyboardInterrupt from a general-mode inline-approval
                # `cancel` (or Ctrl+C at that prompt) needs the same
                # transcript-recording exit path as ESC during streaming.
                try:
                    tool_results = [
                        self._execute_tool(call["name"], call["input"], call["id"], phase)
                        for call in tool_calls
                    ]
                except (InterruptedError, KeyboardInterrupt):
                    self._handle_interrupt(phase)
                    return
                self.conversation.append({"role": "user", "content": tool_results})
                # Tools have run — record what executed so the next LLM
                # call's audit row attributes its token spend.
                self._last_tools_run = [call["name"] for call in tool_calls]

                # Resume hand-off: if the LLM called request_resume, end
                # this run after the confirmation tool_result is recorded.
                # The REPL looks at self.resume_requested next.
                if self.resume_requested is not None:
                    logger.info(
                        "Agent loop ending — resume requested for task %s",
                        self.resume_requested,
                    )
                    break

                if response.stop_reason == "end_turn":
                    logger.info("Agent signaled end_turn in %s phase", phase)
                    break

            else:
                console.print(
                    f"[bold red]Agent hit max iterations ({self.max_iterations}) "
                    f"in {phase} phase.[/bold red]"
                )

    def _handle_interrupt(self, phase: str) -> None:
        """Handle an ESC or Ctrl+C during streaming. Records the cancel
        in the transcript so a follow-up message has the model's last
        partial output as context, and exits the loop cleanly.
        """
        # Add a blank line so the next prompt isn't glued to a partial token.
        console.print()
        console.print(
            f"[yellow]Interrupted during {phase} phase. "
            "Task state is persisted; the next message can redirect "
            "or resume.[/yellow]"
        )
        self.conversation.append({
            "role": "user",
            "content": "<user pressed ESC; the previous agent message "
                       "was interrupted mid-stream. Acknowledge briefly "
                       "and wait for the next instruction.>",
        })

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
        render_tool_call_header(tool_name, tool_input)

        # submit_plan is intercepted here — never reaches the registry executor.
        # In non-plan modes we still intercept it (defensive: stale context
        # may cause the LLM to call it anyway) and return a clarifying
        # tool_result so the loop doesn't crash and the LLM can self-correct.
        if tool_name == "submit_plan":
            if self.mode != AgentMode.PLAN:
                msg = (
                    f"submit_plan is not used in {self.mode.value} mode — "
                    "skip the planning ceremony and proceed directly with "
                    "the request. See the MODE OVERRIDE block at the top "
                    "of your system prompt."
                )
                render_tool_blocked(tool_name, msg)
                return {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": f"ERROR: {msg}",
                    "is_error": True,
                }
            return self._handle_submit_plan(tool_input, tool_use_id)

        # request_resume is also intercepted: record the signal, return a
        # synthetic success, and let the loop body see resume_requested
        # on the next iteration check so it stops cleanly.
        if tool_name == "request_resume":
            return self._handle_request_resume(tool_input, tool_use_id)

        # ----------------------------------------------------------------
        # Mode-aware write gating. Plan mode keeps today's two-stage gate
        # (no writes during planning, no writes without plan approval).
        # Execution mode passes writes through unconditionally. General
        # mode prompts the user inline per write.
        # ----------------------------------------------------------------

        if tool_name in WRITE_TOOLS and self.mode == AgentMode.PLAN:
            if phase == "planning":
                msg = (
                    f"Tool '{tool_name}' is a write operation and cannot execute "
                    "during planning. Include it as a step in the execution plan."
                )
                render_tool_blocked(tool_name, msg)
                return {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": f"ERROR: {msg}",
                    "is_error": True,
                }
            if not self.plan_approved:
                msg = f"Tool '{tool_name}' requires an approved plan before execution."
                render_tool_blocked(tool_name, msg)
                return {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": f"ERROR: {msg}",
                    "is_error": True,
                }

        if tool_name in WRITE_TOOLS and self.mode == AgentMode.GENERAL:
            approved = self._request_inline_write_approval(tool_name, tool_input)
            if not approved:
                msg = (
                    f"User declined the inline approval for '{tool_name}'. "
                    "Suggest an alternative read-only approach or ask the "
                    "user what they would like instead."
                )
                render_tool_blocked(tool_name, msg)
                return {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": f"ERROR: {msg}",
                    "is_error": True,
                }

        # Execution mode: writes pass through with no gate.

        # v2 slice 1 — capture pre-write content for inline diff rendering.
        # None means "skip diff" (path traversal, read error). "" means
        # "new file" — diff renders as all additions.
        diff_before: str | None = None
        if tool_name == "file_write":
            diff_before = _capture_file_write_before(tool_input)

        try:
            with tool_status(tool_name):
                result = self.tool_registry.execute(tool_name, tool_input)
            result_str = json.dumps(result) if isinstance(result, dict) else str(result)
            if (
                tool_name == "file_write"
                and diff_before is not None
                and isinstance(result, dict)
                and not result.get("error")
            ):
                try:
                    render_file_write_diff(
                        str(tool_input.get("file_path", "")),
                        diff_before,
                        str(tool_input.get("content", "")),
                    )
                except Exception:
                    logger.exception("Could not render file_write diff")

            # Auto-reindex hook: after a successful file_write or
            # sf_source_deploy, parse the affected file(s) into the
            # local SQLite index so subsequent retrieval finds them.
            # Best-effort: never break the tool flow on a reindex error.
            self._auto_reindex_after_write(tool_name, tool_input, result)

            render_tool_ok(tool_name, result_str, tool_use_id=tool_use_id)
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": result_str,
            }
        except Exception as e:
            error_msg = f"{type(e).__name__}: {e}"
            render_tool_error(tool_name, error_msg, tool_use_id=tool_use_id)
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": f"ERROR: {error_msg}",
                "is_error": True,
            }

    def _mode_filtered_tool_definitions(self) -> list[dict[str, Any]]:
        """Tool defs the LLM sees, with submit_plan hidden in non-plan modes.

        Plan mode keeps the full registry. Execution + general modes drop
        `submit_plan` so the LLM doesn't try to call a tool that's no-op
        for them. The defensive intercept in `_execute_tool` still handles
        the case where stale context causes a call to slip through.
        """
        defs = self.tool_registry.get_tool_definitions()
        if self.mode == AgentMode.PLAN:
            return defs
        return [d for d in defs if d.get("name") != "submit_plan"]

    def _request_inline_write_approval(
        self, tool_name: str, tool_input: dict[str, Any]
    ) -> bool:
        """Per-write approval prompt for general mode.

        Choices:
          yes      — approve this single call
          no       — deny this single call (default)
          always   — approve this tool name for the rest of this allowlist's
                     scope. Per-task by default; per-session when the REPL
                     passes a shared set in via `write_allowlist=`.
          cancel   — deny + abort the agent loop (raises KeyboardInterrupt
                     so the existing handler records the cancel and exits
                     the iteration cleanly).

        Safety defaults:
          - Non-TTY input → auto-deny. CI / piped-input runs must never
            silently write; the user gave up no consent in that case.
          - Default choice is `no`. Hitting Enter means safety, not write.
          - Ctrl+C / Ctrl+D at the prompt → KeyboardInterrupt (treated
            like `cancel` so the loop unwinds through the existing
            interrupt handler instead of swallowing the signal).
        """
        import sys

        if tool_name in self._write_allowlist:
            console.print(
                f"  [dim]auto-approved (allowlist): {tool_name}[/dim]"
            )
            return True

        if not sys.stdin.isatty():
            logger.warning(
                "general-mode inline approval auto-denied (non-TTY input): %s",
                tool_name,
            )
            return False

        # Surface what's about to run so the user can decide on the body,
        # not just the tool name.
        from sf_dev_agent.repl_ui import format_tool_input_summary
        summary = format_tool_input_summary(tool_input)
        console.print(Panel(
            f"[bold]Tool:[/bold] [cyan]{tool_name}[/cyan]\n"
            f"[bold]Input:[/bold] [dim]{summary}[/dim]",
            title="[bold yellow]Write approval requested[/bold yellow]",
            border_style="yellow",
        ))

        try:
            choice = Prompt.ask(
                "[bold yellow]Allow this write?[/bold yellow]",
                choices=["yes", "no", "always", "cancel"],
                default="no",
            )
        except (EOFError, KeyboardInterrupt):
            raise KeyboardInterrupt(
                "user cancelled at general-mode inline approval"
            ) from None

        if choice == "always":
            self._write_allowlist.add(tool_name)
            console.print(
                f"  [green]allowlisted[/green] [cyan]{tool_name}[/cyan] "
                "for this session"
            )
            return True
        if choice == "cancel":
            raise KeyboardInterrupt(
                "user cancelled at general-mode inline approval"
            )
        return choice == "yes"

    def _auto_reindex_after_write(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        result: Any,
    ) -> None:
        """Post-success reindex hook for write tools.

        Resolves the set of files affected by `tool_name` and feeds them
        to `_reindex_files_after_write`, then renders a one-line summary
        when something was indexed. Never raises — every failure mode
        bubbles up as a logged exception inside the helper.

        Currently hooks:
          - `file_write` → reindex the single written file (resolved
            from the executor's `result["path"]`).
          - `sf_source_deploy` → walk every file under the deploy
            `source_path` and reindex any with a registered parser.
        """
        if not isinstance(result, dict) or result.get("error"):
            return

        paths: list[Path] = []
        if tool_name == "file_write":
            written = result.get("path")
            if isinstance(written, str) and written:
                paths.append(Path(written))
        elif tool_name == "sf_source_deploy":
            src = tool_input.get("source_path")
            if isinstance(src, str) and src:
                try:
                    from sf_dev_agent.paths import agent_workspace
                    base = (agent_workspace() / src).resolve()
                    if base.is_dir():
                        paths = [p for p in base.rglob("*") if p.is_file()]
                    elif base.is_file():
                        paths = [base]
                except Exception:
                    logger.exception(
                        "Could not resolve sf_source_deploy source_path %r",
                        src,
                    )

        if not paths:
            return

        mock_org = bool(getattr(self.tool_registry, "mock_org", False))
        try:
            summary = _reindex_files_after_write(paths, mock_org=mock_org)
        except Exception:
            logger.exception("Auto-reindex hook crashed")
            return

        if summary["components"] or summary["embedded"]:
            try:
                render_reindex_summary(**summary)
            except Exception:
                logger.exception("Could not render reindex summary")


    # ------------------------------------------------------------------
    # Plan presentation and approval
    # ------------------------------------------------------------------

    def _handle_request_resume(
        self, tool_input: dict[str, Any], tool_use_id: str
    ) -> dict[str, Any]:
        """Record the resume signal so the REPL can pick it up.

        We don't actually swap into the resumed task here — that's the
        REPL's job once `run()` returns. We just stamp the requested
        task_id on `self.resume_requested` and return a tool_result so
        the model sees a clean confirmation (not an error).
        """
        task_id = tool_input.get("task_id")
        if not task_id:
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": "ERROR: request_resume requires a task_id",
                "is_error": True,
            }

        # Defensive: confirm the task exists + is in scope, so we don't
        # set the signal on a typo and confuse the REPL.
        if self.working_memory is not None:
            row = self.working_memory.get_task(task_id)
            if row is None:
                return {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": f"ERROR: task {task_id!r} not found",
                    "is_error": True,
                }
            if row.tenant_id != self.org.tenant_id:
                return {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": (
                        f"ERROR: task {task_id!r} belongs to a different "
                        "tenant; cannot resume."
                    ),
                    "is_error": True,
                }

        self.resume_requested = task_id
        rationale = tool_input.get("rationale", "")
        console.print(
            f"  [cyan]Resume requested[/cyan] -> "
            f"task={task_id} rationale={rationale!r}"
        )
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": json.dumps({
                "resume_signaled": True,
                "task_id": task_id,
                "next": "REPL will hand off to AgentLoop.resume()",
            }),
        }

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
