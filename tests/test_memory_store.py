"""Unit tests for MemoryStore — schema, save/recall, hash-gated re-embed,
scope filter, supersedes link, and tool wiring through ToolRegistry.

Uses MockEmbedder throughout — no live API calls.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from sf_dev_agent.context import MockEmbedder
from sf_dev_agent.memory import (
    MEMORY_TYPES,
    MemoryRecord,
    MemoryScope,
    MemoryStore,
    make_memory_id,
)
from sf_dev_agent.models.schemas import OrgConnection
from sf_dev_agent.tools.registry import ToolRegistry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    db = tmp_path / "memory.db"
    s = MemoryStore(db)
    yield s
    s.close()


@pytest.fixture
def scope() -> MemoryScope:
    return MemoryScope(tenant_id="t1", org_alias="OrgA")


@pytest.fixture
def org() -> OrgConnection:
    return OrgConnection(
        tenant_id="t1",
        org_alias="OrgA",
        org_type="developer",
        instance_url="https://example.salesforce.com",
    )


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------

def test_opening_store_creates_memories_table(tmp_path: Path) -> None:
    """A fresh DB has the memories table after one open. Idempotent."""
    db = tmp_path / "fresh.db"
    with MemoryStore(db) as s:
        rows = s._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memories'"
        ).fetchall()
        assert len(rows) == 1

    # Re-open is a no-op — schema script is idempotent.
    with MemoryStore(db) as s:
        rows = s._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memories'"
        ).fetchall()
        assert len(rows) == 1


def test_opening_alongside_metadata_index_shares_db(tmp_path: Path) -> None:
    """Memory + index live in the same SQLite file (one orchestrator fan-out)."""
    from sf_dev_agent.context import MetadataIndex

    db = tmp_path / "combined.db"

    with MetadataIndex(db) as _:
        pass
    with MemoryStore(db) as _:
        pass

    # Both tables should now exist in the same file.
    with MemoryStore(db) as s:
        names = {
            r["name"] for r in s._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"components", "knowledge_entries", "memories"}.issubset(names)


# ---------------------------------------------------------------------------
# Save / round-trip
# ---------------------------------------------------------------------------

def test_save_inserts_then_returns_record(store: MemoryStore, scope: MemoryScope) -> None:
    record = store.save(
        scope=scope,
        type="feedback",
        name="prefer-bundled-pr",
        description="User prefers single bundled PR for refactors",
        body=(
            "Rule: bundle small refactor PRs.\n"
            "**Why:** user said splitting was churn.\n"
            "**How to apply:** for refactors in this area, prefer one PR."
        ),
        tags=["pr-style"],
        source_session_id="sess-1",
    )
    assert isinstance(record, MemoryRecord)
    assert record.type == "feedback"
    assert record.tenant_id == "t1"
    assert record.org_alias == "OrgA"
    assert record.access_count == 0
    assert record.tags == ["pr-style"]
    assert record.source_session_id == "sess-1"
    assert record.id == make_memory_id(scope, "prefer-bundled-pr")


def test_save_same_id_replaces_body_and_drops_embedding(
    store: MemoryStore, scope: MemoryScope
) -> None:
    """Re-saving the same name updates body and forces re-embed."""
    record1 = store.save(
        scope=scope,
        type="project",
        name="merge-freeze",
        description="merge freeze starts 2026-03-05",
        body="freeze for mobile release cut",
    )
    # Pretend this row was already embedded.
    blob = np.asarray([1.0] * 64, dtype=np.float32).tobytes()
    store._conn.execute(
        "UPDATE memories SET embedding = ?, embedded_text_hash = 'old' WHERE id = ?",
        (blob, record1.id),
    )
    store._conn.commit()

    record2 = store.save(
        scope=scope,
        type="project",
        name="merge-freeze",
        description="merge freeze EXTENDED to 2026-03-12",
        body="freeze extended; mobile release pushed",
    )
    assert record2.id == record1.id
    assert "EXTENDED" in record2.description

    # Embedding was dropped — body changed, so the stored vector is stale.
    row = store._conn.execute(
        "SELECT embedding, embedded_text_hash FROM memories WHERE id = ?",
        (record2.id,),
    ).fetchone()
    assert row["embedding"] is None
    assert row["embedded_text_hash"] is None


def test_save_rejects_unknown_type(store: MemoryStore, scope: MemoryScope) -> None:
    with pytest.raises(ValueError):
        store.save(
            scope=scope, type="lore", name="x", description="x", body="x",
        )


def test_save_rejects_blank_required_fields(
    store: MemoryStore, scope: MemoryScope
) -> None:
    with pytest.raises(ValueError):
        store.save(scope=scope, type="user", name=" ", description="x", body="x")
    with pytest.raises(ValueError):
        store.save(scope=scope, type="user", name="x", description="x", body=" ")


def test_save_requires_tenant(store: MemoryStore) -> None:
    with pytest.raises(ValueError):
        store.save(
            scope=MemoryScope(tenant_id=""),
            type="user", name="x", description="x", body="x",
        )


# ---------------------------------------------------------------------------
# Embedding pipeline
# ---------------------------------------------------------------------------

def test_embed_pending_only_embeds_new_or_changed(
    store: MemoryStore, scope: MemoryScope
) -> None:
    embedder = MockEmbedder(dim=64)

    store.save(scope=scope, type="user", name="role",
               description="user is staff eng", body="staff engineer")
    store.save(scope=scope, type="project", name="freeze",
               description="freeze 2026-03-05", body="mobile freeze")

    first = store.embed_pending(embedder)
    assert first.embedded == 2
    assert first.skipped_unchanged == 0

    second = store.embed_pending(embedder)
    assert second.embedded == 0
    assert second.skipped_unchanged == 2

    # Force re-embed works.
    forced = store.embed_pending(embedder, force=True)
    assert forced.embedded == 2


# ---------------------------------------------------------------------------
# Recall — ranking, scope, type filter
# ---------------------------------------------------------------------------

def test_recall_returns_hits_ranked_by_similarity(
    store: MemoryStore, scope: MemoryScope
) -> None:
    embedder = MockEmbedder(dim=64)
    store.save(scope=scope, type="feedback", name="duplicate-detection",
               description="bulk duplicate account detection rules",
               body="Use Email__c + Phone match to find duplicate accounts.")
    store.save(scope=scope, type="feedback", name="tax-calc",
               description="invoice tax calculation",
               body="Compute tax for invoices using regional tax rules.")
    store.embed_pending(embedder)

    query_vec = embedder.embed_one("duplicate account detection")
    hits = store.recall(query_embedding=query_vec, scope=scope, limit=5)

    assert len(hits) >= 1
    # Top hit must be the duplicate-detection memory (not the tax one).
    assert hits[0].record.name == "duplicate-detection"


def test_recall_bumps_access_counter_and_timestamp(
    store: MemoryStore, scope: MemoryScope
) -> None:
    embedder = MockEmbedder(dim=64)
    store.save(scope=scope, type="user", name="x",
               description="user role", body="staff engineer")
    store.embed_pending(embedder)

    before = store.find_by_id(make_memory_id(scope, "x"))
    assert before.access_count == 0

    query_vec = embedder.embed_one("user role")
    hits = store.recall(query_embedding=query_vec, scope=scope, limit=5)
    assert hits

    after = store.find_by_id(hits[0].record.id)
    assert after.access_count == 1
    assert after.last_accessed_at >= before.last_accessed_at


def test_recall_filters_by_type(store: MemoryStore, scope: MemoryScope) -> None:
    embedder = MockEmbedder(dim=64)
    store.save(scope=scope, type="user", name="role",
               description="senior dev", body="senior engineer")
    store.save(scope=scope, type="feedback", name="prefer-bulk",
               description="prefer bulk operations", body="prefer bulk inserts")
    store.embed_pending(embedder)

    query_vec = embedder.embed_one("engineering preference")
    user_hits = store.recall(query_embedding=query_vec, scope=scope, type="user")
    assert all(h.record.type == "user" for h in user_hits)

    feedback_hits = store.recall(
        query_embedding=query_vec, scope=scope, type="feedback",
    )
    assert all(h.record.type == "feedback" for h in feedback_hits)


def test_recall_scope_strict_on_tenant(store: MemoryStore) -> None:
    """Memories from another tenant must not leak in."""
    embedder = MockEmbedder(dim=64)
    s1 = MemoryScope(tenant_id="t1", org_alias="OrgA")
    s2 = MemoryScope(tenant_id="t2", org_alias="OrgZ")

    store.save(scope=s1, type="user", name="t1-secret",
               description="t1 only", body="tenant 1 only")
    store.save(scope=s2, type="user", name="t2-secret",
               description="t2 only", body="tenant 2 only")
    store.embed_pending(embedder)

    query_vec = embedder.embed_one("only")
    hits = store.recall(query_embedding=query_vec, scope=s1, limit=10)
    assert all(h.record.tenant_id == "t1" for h in hits)
    assert any(h.record.name == "t1-secret" for h in hits)
    assert not any(h.record.name == "t2-secret" for h in hits)


def test_recall_org_alias_includes_cross_org_rows(store: MemoryStore) -> None:
    """An org_alias=NULL row is shared across all orgs in the tenant."""
    embedder = MockEmbedder(dim=64)
    cross = MemoryScope(tenant_id="t1", org_alias=None)
    org_a = MemoryScope(tenant_id="t1", org_alias="OrgA")
    org_b = MemoryScope(tenant_id="t1", org_alias="OrgB")

    store.save(scope=cross, type="user", name="cross",
               description="user role", body="staff engineer everywhere")
    store.save(scope=org_a, type="project", name="a-only",
               description="org A project", body="OrgA-specific work")
    store.embed_pending(embedder)

    query_vec = embedder.embed_one("staff engineer")
    a_hits = store.recall(query_embedding=query_vec, scope=org_a, limit=10)
    b_hits = store.recall(query_embedding=query_vec, scope=org_b, limit=10)

    # Both orgs see the cross-org memory; only OrgA sees the a-only one.
    assert any(h.record.name == "cross" for h in a_hits)
    assert any(h.record.name == "cross" for h in b_hits)
    assert any(h.record.name == "a-only" for h in a_hits)
    assert not any(h.record.name == "a-only" for h in b_hits)


def test_recall_excludes_superseded_by_default(
    store: MemoryStore, scope: MemoryScope
) -> None:
    embedder = MockEmbedder(dim=64)
    old = store.save(scope=scope, type="project", name="old-decision",
                     description="OLD: prefer Flow", body="old guidance")
    new = store.save(scope=scope, type="project", name="new-decision",
                     description="NEW: prefer Apex", body="new guidance")
    store.supersede(old.id, new.id)
    store.embed_pending(embedder)

    query_vec = embedder.embed_one("prefer guidance")
    hits = store.recall(query_embedding=query_vec, scope=scope, limit=10)
    names = {h.record.name for h in hits}
    assert "old-decision" not in names
    assert "new-decision" in names

    # Opt-in surfaces the old one.
    hits_inc = store.recall(
        query_embedding=query_vec, scope=scope, limit=10, include_superseded=True,
    )
    names_inc = {h.record.name for h in hits_inc}
    assert "old-decision" in names_inc


# ---------------------------------------------------------------------------
# list / stats
# ---------------------------------------------------------------------------

def test_list_returns_records_in_scope(store: MemoryStore, scope: MemoryScope) -> None:
    store.save(scope=scope, type="user", name="a",
               description="a", body="a")
    store.save(scope=scope, type="feedback", name="b",
               description="b", body="b")
    rows = store.list(scope=scope)
    names = {r.name for r in rows}
    assert names == {"a", "b"}


def test_stats_counts_by_type(store: MemoryStore, scope: MemoryScope) -> None:
    store.save(scope=scope, type="user", name="a", description="a", body="a")
    store.save(scope=scope, type="user", name="b", description="b", body="b")
    store.save(scope=scope, type="feedback", name="c", description="c", body="c")
    stats = store.stats(scope=scope)
    assert stats == {"user": 2, "feedback": 1}


# ---------------------------------------------------------------------------
# Type taxonomy lock
# ---------------------------------------------------------------------------

def test_memory_types_locked() -> None:
    """The taxonomy is verbatim from Claude Code; extension is intentional."""
    assert MEMORY_TYPES == frozenset({"user", "feedback", "project", "reference"})


# ---------------------------------------------------------------------------
# Tool-registry wiring
# ---------------------------------------------------------------------------

def test_memory_tools_registered(org: OrgConnection) -> None:
    registry = ToolRegistry(org=org, mock_org=True)
    names = {t["name"] for t in registry.get_tool_definitions()}
    assert {"memory_save", "memory_recall", "memory_list"}.issubset(names)


def test_memory_recall_tool_mock_mode_returns_canned(org: OrgConnection) -> None:
    """memory_recall is in _SF_TOOLS — mock mode short-circuits the embed."""
    registry = ToolRegistry(org=org, mock_org=True)
    response = registry.execute("memory_recall", {"query": "anything"})
    assert response["mocked"] is True
    assert response["query"] == "anything"
    assert response["results"] == []


def test_memory_save_round_trips_through_registry(
    tmp_path: Path, org: OrgConnection
) -> None:
    """Save + list + recall via the actual tool surface (non-mock)."""
    db = tmp_path / "memory_via_registry.db"
    registry = ToolRegistry(org=org, mock_org=False, index_db_path=db)

    save_resp = registry.execute("memory_save", {
        "type": "feedback",
        "name": "test-feedback",
        "description": "verifies round-trip via tools",
        "body": "Rule: tools work end-to-end.",
    })
    assert save_resp["saved"] is True
    assert save_resp["tenant_id"] == "t1"
    assert save_resp["org_alias"] == "OrgA"

    list_resp = registry.execute("memory_list", {})
    assert list_resp["count"] == 1
    assert list_resp["memories"][0]["name"] == "test-feedback"


def test_memory_save_invalid_type_returns_error(
    tmp_path: Path, org: OrgConnection
) -> None:
    db = tmp_path / "err.db"
    registry = ToolRegistry(org=org, mock_org=False, index_db_path=db)

    resp = registry.execute("memory_save", {
        "type": "lore",  # not in MEMORY_TYPES
        "name": "x", "description": "x", "body": "x",
    })
    assert "error" in resp


def test_memory_save_cross_org_drops_org_alias(
    tmp_path: Path, org: OrgConnection
) -> None:
    db = tmp_path / "cross.db"
    registry = ToolRegistry(org=org, mock_org=False, index_db_path=db)
    resp = registry.execute("memory_save", {
        "type": "user", "name": "global-pref",
        "description": "applies tenant-wide", "body": "user prefs",
        "cross_org": True,
    })
    assert resp["saved"] is True
    assert resp["org_alias"] is None


# ---------------------------------------------------------------------------
# Decay scoring (Wave 8 slice 2c)
# ---------------------------------------------------------------------------

def _age_memory_in_days(store: MemoryStore, memory_id: str, age_days: float) -> None:
    """Backdate the row's last_accessed_at so decay tests don't have to wait."""
    from datetime import UTC, datetime, timedelta
    backdated = (datetime.now(UTC) - timedelta(days=age_days)).isoformat(
        timespec="seconds"
    )
    store._conn.execute(
        "UPDATE memories SET last_accessed_at = ? WHERE id = ?",
        (backdated, memory_id),
    )
    store._conn.commit()


def test_decay_off_preserves_pure_cosine(
    store: MemoryStore, scope: MemoryScope
) -> None:
    """decay=False keeps ranking driven only by cosine similarity."""
    embedder = MockEmbedder(dim=64)
    fresh = store.save(scope=scope, type="feedback", name="fresh-match",
                       description="exact match", body="duplicate account email phone")
    stale = store.save(scope=scope, type="feedback", name="stale-match",
                       description="exact match", body="duplicate account email phone")
    store.embed_pending(embedder)

    # Stale row hasn't been touched in a year.
    _age_memory_in_days(store, stale.id, age_days=365)

    # Same content -> cosine ties; without decay, the order is determined
    # by numpy's argsort on equal values (stable).
    query_vec = embedder.embed_one("duplicate account email phone")
    pure = store.recall(query_vec, scope=scope, limit=2, decay=False)
    assert {h.record.id for h in pure} == {fresh.id, stale.id}
    # Pure cosine ties — both scores equal.
    assert abs(pure[0].score - pure[1].score) < 1e-6


def test_decay_penalizes_stale_memory_over_fresh(
    store: MemoryStore, scope: MemoryScope
) -> None:
    """At equal cosine, the stale row scores lower than the fresh row."""
    embedder = MockEmbedder(dim=64)
    fresh = store.save(scope=scope, type="feedback", name="fresh",
                       description="dup detection", body="duplicate account email phone")
    stale = store.save(scope=scope, type="feedback", name="stale",
                       description="dup detection", body="duplicate account email phone")
    store.embed_pending(embedder)
    _age_memory_in_days(store, stale.id, age_days=365)

    query_vec = embedder.embed_one("duplicate account email phone")
    hits = store.recall(query_vec, scope=scope, limit=2)  # decay default on

    by_id = {h.record.id: h.score for h in hits}
    assert by_id[fresh.id] > by_id[stale.id], (
        "Fresh memory must outscore stale memory at equal cosine"
    )
    # Stale penalty caps at ~10% — sanity check magnitude.
    assert by_id[fresh.id] - by_id[stale.id] <= 0.15


def test_decay_boosts_high_usage_over_low_usage(
    store: MemoryStore, scope: MemoryScope
) -> None:
    """At equal cosine + same age, more accesses ranks higher."""
    embedder = MockEmbedder(dim=64)
    cold = store.save(scope=scope, type="feedback", name="cold",
                      description="dup detection", body="duplicate account email phone")
    hot = store.save(scope=scope, type="feedback", name="hot",
                     description="dup detection", body="duplicate account email phone")
    store.embed_pending(embedder)

    # Manually set access_count without changing last_accessed_at.
    store._conn.execute(
        "UPDATE memories SET access_count = ? WHERE id = ?", (10, hot.id),
    )
    store._conn.commit()

    query_vec = embedder.embed_one("duplicate account email phone")
    hits = store.recall(query_vec, scope=scope, limit=2)

    by_id = {h.record.id: h.score for h in hits}
    assert by_id[hot.id] > by_id[cold.id], (
        "Hot (high access_count) memory must outscore cold one at equal cosine"
    )
    # Usage boost caps at ~5%.
    assert by_id[hot.id] - by_id[cold.id] <= 0.10


def test_decay_keeps_score_in_unit_range(
    store: MemoryStore, scope: MemoryScope
) -> None:
    """Adjusted scores must remain in [0, 1] — orchestrator contract."""
    embedder = MockEmbedder(dim=64)
    store.save(scope=scope, type="user", name="hot",
               description="x", body="content here")
    store.embed_pending(embedder)

    # Crank access_count well past saturation.
    store._conn.execute(
        "UPDATE memories SET access_count = 1000",
    )
    store._conn.commit()

    query_vec = embedder.embed_one("content here")
    hits = store.recall(query_vec, scope=scope, limit=5)
    for h in hits:
        assert 0.0 <= h.score <= 1.0


def test_decay_cosine_dominates_over_decay_signal(
    store: MemoryStore, scope: MemoryScope
) -> None:
    """A high-cosine stale memory must still beat a low-cosine fresh one."""
    embedder = MockEmbedder(dim=64)
    relevant_stale = store.save(
        scope=scope, type="feedback", name="relevant-but-old",
        description="duplicate account detection",
        body="duplicate account detection email phone match",
    )
    store.save(
        scope=scope, type="feedback", name="unrelated-fresh",
        description="invoice tax", body="invoice tax calculation regional rules",
    )
    store.embed_pending(embedder)
    _age_memory_in_days(store, relevant_stale.id, age_days=365)

    query_vec = embedder.embed_one("duplicate account detection email phone match")
    hits = store.recall(query_vec, scope=scope, limit=5)
    assert hits[0].record.id == relevant_stale.id, (
        "Cosine relevance must dominate the small decay penalty"
    )


def test_decay_via_orchestrator_layer(
    tmp_path: Path, scope: MemoryScope
) -> None:
    """retrieve_context's memory layer inherits decay from MemoryStore.recall."""
    from sf_dev_agent.context import retrieve_context

    db = tmp_path / "decay_orch.db"
    embedder = MockEmbedder(dim=64)
    with MemoryStore(db) as store:
        store.save(scope=scope, type="feedback", name="fresh-pref",
                   description="dup detection", body="duplicate account email")
        stale = store.save(scope=scope, type="feedback", name="stale-pref",
                           description="dup detection", body="duplicate account email")
        store.embed_pending(embedder)
        _age_memory_in_days(store, stale.id, age_days=365)

    # Need an empty index for retrieve_context's existence check.
    from sf_dev_agent.context import MetadataIndex
    MetadataIndex(db).close()

    result = retrieve_context(
        query="duplicate account email",
        db_path=db,
        embedder=embedder,
        memory_scope=scope,
        max_tokens=8000,
    )
    mem_hits = [h for h in result.hits if h.source == "memory"]
    assert len(mem_hits) == 2
    by_title = {h.title: h.score for h in mem_hits}
    assert by_title["fresh-pref"] > by_title["stale-pref"]
