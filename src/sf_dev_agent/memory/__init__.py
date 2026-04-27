"""Stateful memory tiers.

Wave 8 slice 1 shipped **project memory** — durable, vector-recalled,
scoped per (tenant, org). Slice 2a adds **working memory** — task state +
conversation transcript persistence so the agent can resume from crash.
Learning memory (cross-tenant promotion) remains deferred to slice 3.

Public API — project memory:
    MemoryStore(db_path)
        .save(scope, type, name, description, body, ...)
        .recall(query_embedding, scope, type=None, limit=10)
        .list(scope, type=None, limit=50)
        .find_by_id(memory_id)
        .supersede(old_id, new_id)
        .embed_pending(embedder, force=False)
        .stats(scope=None)

    MemoryScope(tenant_id, org_alias=None)
    MemoryRecord, MemoryRecallHit, MemoryEmbedResult
    MEMORY_TYPES   # frozenset {"user", "feedback", "project", "reference"}
    make_memory_id(scope, name)

Public API — working memory:
    WorkingMemoryStore(db_path)
        .create_task(task_id, scope, user_request, status="planning")
        .update_task_status(task_id, status, error=None)
        .set_plan(task_id, plan_json)
        .set_plan_approved(task_id, approved=True)
        .set_result(task_id, result_json, status, error=None)
        .get_task(task_id)
        .list_tasks(scope, status=None, limit=50)
        .append_message(task_id, role, content) -> seq
        .load_messages(task_id) -> list[dict]
        .delete_task(task_id)
        .stats(scope=None)

    TaskRow, TERMINAL_STATUSES

    ConversationLog(task_id, store=None, seed=None)
        # list-shaped wrapper that mirrors append() to WorkingMemoryStore
        .append(message)
        .as_messages() -> list[dict]
        # also supports iter, len, indexing, bool
"""

from __future__ import annotations

from sf_dev_agent.memory.conversation_log import ConversationLog
from sf_dev_agent.memory.store import (
    MEMORY_TYPES,
    MemoryEmbedResult,
    MemoryRecallHit,
    MemoryRecord,
    MemoryScope,
    MemoryStore,
    MergeCandidate,
    make_memory_id,
)
from sf_dev_agent.memory.working import (
    TERMINAL_STATUSES,
    TaskRow,
    WorkingMemoryStore,
)

__all__ = [
    "MEMORY_TYPES",
    "TERMINAL_STATUSES",
    "ConversationLog",
    "MemoryEmbedResult",
    "MemoryRecallHit",
    "MemoryRecord",
    "MemoryScope",
    "MemoryStore",
    "MergeCandidate",
    "TaskRow",
    "WorkingMemoryStore",
    "make_memory_id",
]
