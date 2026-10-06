"""Embedder selection, fastembed adapter (faked, no download) and the mismatch guard."""

from __future__ import annotations

import sys
import types

import numpy as np
import pytest
from sf_context_engine import create_embedder, embedder_guard
from sf_context_engine.embedders.base import MockEmbedder
from sf_context_engine.service import ContextService


class _Fake384(MockEmbedder):
    name = "fake:384"

    def __init__(self) -> None:
        super().__init__(dim=384)


# ----------------------------------------------------------------- selection


def test_env_selects_embedder(monkeypatch):
    monkeypatch.setenv("SF_CONTEXT_EMBEDDER", "mock")
    assert create_embedder().name == "mock:hashbow"


def test_unknown_embedder_name_is_rejected(monkeypatch):
    monkeypatch.setenv("SF_CONTEXT_EMBEDDER", "nope")
    with pytest.raises(ValueError, match="Unknown embedder"):
        create_embedder()


@pytest.fixture
def fake_fastembed(monkeypatch):
    calls = {"loaded": 0, "query": 0, "doc": 0}

    class TextEmbedding:
        def __init__(self, model_name, **kw):
            calls["loaded"] += 1
            self.model_name = model_name

        @staticmethod
        def list_supported_models():
            return [{"model": "BAAI/bge-small-en-v1.5", "dim": 384}]

        def embed(self, texts):
            calls["doc"] += 1
            return [np.full(384, 2.0, dtype=np.float32) for _ in texts]

        def query_embed(self, texts):
            calls["query"] += 1
            return [np.full(384, 3.0, dtype=np.float32) for _ in texts]

    mod = types.ModuleType("fastembed")
    mod.TextEmbedding = TextEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", mod)
    return calls


def test_fastembed_is_lazy_normalised_and_query_aware(monkeypatch, fake_fastembed):
    monkeypatch.setenv("SF_CONTEXT_EMBEDDER", "fastembed")
    emb = create_embedder()
    assert emb.name == "fastembed:BAAI/bge-small-en-v1.5" and emb.dim == 384
    assert fake_fastembed["loaded"] == 0  # model not loaded until first embed
    vec = emb.embed_one("hello")
    assert fake_fastembed["loaded"] == 1 and fake_fastembed["doc"] == 1
    assert abs(float(np.linalg.norm(vec)) - 1.0) < 1e-5
    q = create_embedder(task_type="RETRIEVAL_QUERY")
    q.embed_one("hello")
    assert fake_fastembed["query"] == 1


def test_fastembed_unsupported_model_errors(monkeypatch, fake_fastembed):
    monkeypatch.setenv("SF_CONTEXT_EMBEDDER", "fastembed")
    monkeypatch.setenv("SF_CONTEXT_EMBEDDER_MODEL", "not/a-model")
    with pytest.raises(ValueError, match="not supported"):
        create_embedder()


# --------------------------------------------------------------------- guard


def test_claim_records_then_detects_model_switch(tmp_path):
    db = tmp_path / "g.sqlite"
    mock, other = MockEmbedder(), _Fake384()
    assert embedder_guard.check(db, mock).state == "unset"
    assert embedder_guard.claim(db, mock).state == "ok"
    assert embedder_guard.check(db, other).mismatch
    with pytest.raises(embedder_guard.EmbedderMismatch):
        embedder_guard.claim(db, other)


def test_reset_discards_vectors_and_adopts_new_embedder(tmp_path, monkeypatch):
    db = tmp_path / "g.sqlite"
    svc = ContextService(org_alias="O", tenant_id="t", db_path=db)
    assert svc.embed_knowledge_base()["embedded"] > 0  # records mock:hashbow (64-d)

    monkeypatch.setattr("sf_context_engine.create_embedder", lambda **kw: _Fake384())
    blocked = svc.embed_knowledge_base()
    assert blocked["error"] == "embedder_mismatch"
    assert svc.knowledge_search("soql in a loop")["error"] == "embedder_mismatch"

    redone = svc.embed_knowledge_base(reset_embeddings=True)
    assert "error" not in redone and redone["embedded"] > 0
    assert embedder_guard.check(db, _Fake384()).state == "ok"
    assert svc.knowledge_search("soql in a loop")["match_count"] > 0


def test_legacy_db_without_record_is_adopted_when_dimension_matches(tmp_path):
    import sqlite3

    db = tmp_path / "legacy.sqlite"
    svc = ContextService(org_alias="O", tenant_id="t", db_path=db)
    svc.embed_knowledge_base()
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM embedder_meta")
    conn.commit()
    conn.close()
    assert embedder_guard.check(db, MockEmbedder()).state == "legacy_ok"
    assert embedder_guard.check(db, _Fake384()).mismatch


# -------------------------------------------------------------------- memory


def test_saved_memory_is_immediately_recallable(tmp_path):
    svc = ContextService(org_alias="O", tenant_id="t", db_path=tmp_path / "m.sqlite")
    saved = svc.memory_save(
        type="project", name="trigger-policy",
        description="One trigger per object using a handler class",
        body="Use handler classes.\n\n**Why:** testability.\n**How to apply:** always.",
    )
    assert saved["embedded"] is True
    hits = svc.memory_recall("one trigger per object handler")
    assert hits["match_count"] == 1 and hits["results"][0]["name"] == "trigger-policy"
