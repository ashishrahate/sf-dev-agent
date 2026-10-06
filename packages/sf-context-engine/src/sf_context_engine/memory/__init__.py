"""Project memory — durable, vector-recalled, scoped per (tenant, org).

Public API:
    MemoryStore(db_path), MemoryScope(tenant_id, org_alias=None)
    MemoryRecord, MemoryRecallHit, MemoryEmbedResult, MergeCandidate
    MEMORY_TYPES, make_memory_id

Export and promotion helpers live in `sf_context_engine.memory.export`
and `sf_context_engine.memory.promote`.
"""

from __future__ import annotations

from sf_context_engine.memory.store import (
    MEMORY_TYPES,
    MemoryEmbedResult,
    MemoryRecallHit,
    MemoryRecord,
    MemoryScope,
    MemoryStore,
    MergeCandidate,
    make_memory_id,
)

__all__ = [
    "MEMORY_TYPES",
    "MemoryEmbedResult",
    "MemoryRecallHit",
    "MemoryRecord",
    "MemoryScope",
    "MemoryStore",
    "MergeCandidate",
    "make_memory_id",
]
