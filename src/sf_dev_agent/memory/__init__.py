"""Stateful memory tiers.

Wave 8 slice 1 ships **project memory** — durable, vector-recalled, scoped
per (tenant, org). Working memory (conversation persistence) and learning
memory (cross-tenant promotion) are deferred to slices 2 / 3.

Public API:
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
"""

from __future__ import annotations

from sf_dev_agent.memory.store import (
    MEMORY_TYPES,
    MemoryEmbedResult,
    MemoryRecallHit,
    MemoryRecord,
    MemoryScope,
    MemoryStore,
    make_memory_id,
)

__all__ = [
    "MEMORY_TYPES",
    "MemoryEmbedResult",
    "MemoryRecallHit",
    "MemoryRecord",
    "MemoryScope",
    "MemoryStore",
    "make_memory_id",
]
