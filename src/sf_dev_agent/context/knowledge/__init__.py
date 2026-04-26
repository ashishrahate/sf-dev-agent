"""Knowledge base — Salesforce platform knowledge that is NOT org-specific.

Lives alongside the metadata index but tracks a fundamentally different kind
of content: governor limits, anti-patterns, best practices, and architectural
patterns that apply to any Salesforce org. Content ships as Markdown files
under `entries/<category>/*.md` with YAML frontmatter; the bundled set is
authored once and version-controlled, then auto-ingested into SQLite on first
`KnowledgeBase` open.

Public API:
    KnowledgeBase(db_path, entries_dir=None)
        .auto_load_if_empty()        # ingest bundled entries on first open
        .load_entries(entries_dir)   # idempotent re-ingest from disk
        .embed_entries(embedder, force=False)   # hash-gated re-embed
        .search(query_embedding, category=None, limit=10)
        .find_by_id / find_by_category
        .embedding_stats / stats
    KnowledgeEntry              # one entry, hydrated for callers
    KnowledgeSearchHit          # entry + cosine score
    bundled_entries_dir() -> Path
"""

from __future__ import annotations

from sf_dev_agent.context.knowledge.store import (
    KnowledgeBase,
    KnowledgeEntry,
    KnowledgeSearchHit,
    bundled_entries_dir,
)

__all__ = [
    "KnowledgeBase",
    "KnowledgeEntry",
    "KnowledgeSearchHit",
    "bundled_entries_dir",
]
