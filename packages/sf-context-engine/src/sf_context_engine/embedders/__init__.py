"""Pluggable text-embedding backends for the metadata index.

Public API:
    Embedder              # ABC
    MockEmbedder          # deterministic, no-API, for tests
    GeminiEmbedder        # google-genai gemini-embedding-001
    FastEmbedEmbedder     # local ONNX (fastembed), no API key
    create_embedder(provider=None, **kwargs) -> Embedder
        Factory that picks an embedder by name or SF_CONTEXT_EMBEDDER; otherwise
        gemini if GOOGLE_API_KEY is set, else fastembed if installed, else mock.
    hash_text(text) -> str
"""

from __future__ import annotations

import importlib.util
import logging
import os

from sf_context_engine.embedders.base import Embedder, MockEmbedder, hash_text

logger = logging.getLogger(__name__)


def create_embedder(
    provider: str | None = None,
    **kwargs,
) -> Embedder:
    """Resolve an embedder by name, or auto-pick from environment.

    provider="gemini"     -> GeminiEmbedder (requires GOOGLE_API_KEY)
    provider="fastembed"  -> FastEmbedEmbedder (local ONNX, no key; `[local]` extra)
    provider="mock"       -> MockEmbedder (deterministic, for tests)
    provider=None         -> SF_CONTEXT_EMBEDDER if set, else gemini if
                             GOOGLE_API_KEY is set, else fastembed if installed,
                             else mock
    """
    chosen = provider or os.environ.get("SF_CONTEXT_EMBEDDER", "").strip().lower() or _auto_pick()

    if chosen == "gemini":
        from sf_context_engine.embedders.gemini import GeminiEmbedder
        return GeminiEmbedder(**kwargs)

    if chosen == "fastembed":
        from sf_context_engine.embedders.fastembed import FastEmbedEmbedder
        return FastEmbedEmbedder(**kwargs)

    if chosen == "mock":
        return MockEmbedder(**kwargs)

    raise ValueError(
        f"Unknown embedder provider: {chosen!r} (expected gemini, fastembed or mock)"
    )


def _auto_pick() -> str:
    if os.environ.get("GOOGLE_API_KEY"):
        return "gemini"
    if importlib.util.find_spec("fastembed") is not None:
        return "fastembed"
    logger.info("No GOOGLE_API_KEY and fastembed not installed; falling back to MockEmbedder")
    return "mock"


__all__ = [
    "Embedder",
    "MockEmbedder",
    "create_embedder",
    "hash_text",
]
