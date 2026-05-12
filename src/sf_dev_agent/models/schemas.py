"""Core data models for the Salesforce Developer Agent."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Operating mode — chosen at task start, fixed for that task's lifetime
# ---------------------------------------------------------------------------

class AgentMode(str, Enum):
    """How the agent gates write operations.

    PLAN (default) — current behavior. Agent submits a structured plan via
        `submit_plan`; user approves; writes execute as bulk-pre-approved.
        Best for production-touching work.

    EXECUTION — autonomous. No plan ceremony, no per-write approval gate.
        The user has explicitly authorized direct execution for the
        session. Best for trusted scratch/dev work.

    GENERAL — read-only by default with per-write inline approval. The
        agent prefers read tools; if it needs a write, the user is
        prompted (yes/no/always-this-tool/cancel) before it runs. Best
        for Q&A / exploration where occasional consented writes are OK.
    """

    PLAN = "plan"
    EXECUTION = "execution"
    GENERAL = "general"


# ---------------------------------------------------------------------------
# Task state machine
# ---------------------------------------------------------------------------

class TaskStatus(str, Enum):
    RECEIVED = "received"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_USER_INPUT = "awaiting_user_input"  # slice 4: request_user_input pause
    EXECUTING = "executing"
    VALIDATING = "validating"
    COMPLETE = "complete"
    FAILED = "failed"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"


class Task(BaseModel):
    task_id: str
    tenant_id: str
    status: TaskStatus = TaskStatus.RECEIVED
    user_request: str
    plan: ExecutionPlan | None = None
    result: TaskResult | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Execution plan (output of Phase 1)
# ---------------------------------------------------------------------------

class RiskLevel(str, Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class PlanStep(BaseModel):
    step_number: int
    action: str  # tool name — e.g., "sf_source_deploy", "file_write"
    target: str  # what it operates on — e.g., "classes/AccountTriggerHandler.cls"
    mode: str  # "read" | "create" | "modify" | "delete"
    risk: RiskLevel = RiskLevel.LOW
    description: str


class PreflightCheck(BaseModel):
    label: str  # e.g., "Existing triggers on Account"
    result: str  # e.g., "AccountTrigger (before insert, before update)"


class ExecutionPlan(BaseModel):
    summary: str
    preflight_checks: list[PreflightCheck] = []
    steps: list[PlanStep] = []
    risk_assessment: RiskLevel = RiskLevel.LOW
    risk_reasoning: str = ""
    rollback_strategy: str = ""
    components_created: int = 0
    components_modified: int = 0
    components_deleted: int = 0
    test_classes_affected: int = 0


# ---------------------------------------------------------------------------
# Task result (output of Phase 2)
# ---------------------------------------------------------------------------

class TaskResult(BaseModel):
    success: bool
    summary: str
    components_deployed: list[str] = []
    test_results: TestRunResult | None = None
    errors: list[str] = []


class TestRunResult(BaseModel):
    total: int = 0
    passed: int = 0
    failed: int = 0
    coverage_percent: float = 0.0
    failures: list[TestFailure] = []


class TestFailure(BaseModel):
    test_class: str
    test_method: str
    message: str
    stack_trace: str = ""


# ---------------------------------------------------------------------------
# Tool definitions (for the agent's tool_use calls)
# ---------------------------------------------------------------------------

class ToolDefinition(BaseModel):
    """Schema for a tool exposed to the agent via the API."""
    name: str
    description: str
    parameters: dict[str, Any]
    read_only: bool = True  # If False, requires approved plan


# ---------------------------------------------------------------------------
# Conversation / message types
# ---------------------------------------------------------------------------

class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL_RESULT = "tool_result"


class ConversationMessage(BaseModel):
    role: MessageRole
    content: Any  # str or list of content blocks
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Org connection config
# ---------------------------------------------------------------------------

class OrgConnection(BaseModel):
    tenant_id: str
    org_alias: str
    org_type: str  # "sandbox" | "scratch" | "production" | "developer"
    instance_url: str
    api_version: str = "62.0"
    access_token: str = ""  # injected at runtime, never persisted to disk
    refresh_token_ref: str = ""  # reference to secrets vault, not the token itself
