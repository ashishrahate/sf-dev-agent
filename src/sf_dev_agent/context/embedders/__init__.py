"""Pluggable text-embedding backends for the metadata index.

Public API:
    Embedder              # ABC
    MockEmbedder          # deterministic, no-API, for tests
    GeminiEmbedder        # google-genai text-embedding-004
    create_embedder(provider=None, **kwargs) -> Embedder
        Factory that picks an embedder by name; defaults to gemini if
        GOOGLE_API_KEY is present, else mock.
    hash_text(text) -> str
"""

from __future__ import annotations

import logging
import os

from sf_dev_agent.context.embedders.base import Embedder, MockEmbedder, hash_text

logger = logging.getLogger(__name__)


def create_embedder(
    provider: str | None = None,
    **kwargs,
) -> Embedder:
    """Resolve an embedder by name, or auto-pick from environment.

    provider="gemini"  -> GeminiEmbedder (requires GOOGLE_API_KEY)
    provider="mock"    -> MockEmbedder (deterministic, for tests)
    provider=None      -> gemini if GOOGLE_API_KEY set, else mock
    """
    chosen = provider or _auto_pick()

    if chosen == "gemini":
        from sf_dev_agent.context.embedders.gemini import GeminiEmbedder
        return GeminiEmbedder(**kwargs)

    if chosen == "mock":
        return MockEmbedder(**kwargs)

    raise ValueError(f"Unknown embedder provider: {chosen!r}")


def _auto_pick() -> str:
    if os.environ.get("GOOGLE_API_KEY"):
        return "gemini"
    logger.info("No GOOGLE_API_KEY set; falling back to MockEmbedder")
    return "mock"


__all__ = [
    "Embedder",
    "MockEmbedder",
    "create_embedder",
    "hash_text",
]
