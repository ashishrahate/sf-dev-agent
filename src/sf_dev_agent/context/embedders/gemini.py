"""Gemini embedder using the google-genai SDK and the existing GOOGLE_API_KEY.

Model: gemini-embedding-001 (current production model). The `task_type` is set
to RETRIEVAL_DOCUMENT for indexed components and RETRIEVAL_QUERY for ad-hoc
search queries — Gemini optimizes embeddings differently for each side, so
mixing them up degrades recall.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import numpy as np

from sf_dev_agent.context.embedders.base import Embedder

logger = logging.getLogger(__name__)


class GeminiEmbedder(Embedder):
    name = "gemini:gemini-embedding-001"
    dim = 3072  # gemini-embedding-001 native; supports MRL truncation to 1536/768

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gemini-embedding-001",
        task_type: str = "RETRIEVAL_DOCUMENT",
    ) -> None:
        self.model = model
        self.task_type = task_type
        self.api_key = api_key or os.environ.get("GOOGLE_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "GeminiEmbedder requires GOOGLE_API_KEY in env or as api_key arg"
            )

        # Lazy import — keeps the package import path light when no embedding work is happening.
        try:
            from google import genai
        except ImportError as exc:
            raise ImportError(
                "google-genai SDK not installed. "
                "Run: uv sync --extra gemini"
            ) from exc

        self._client = genai.Client(api_key=self.api_key)

    def embed(self, texts: list[str]) -> list[np.ndarray]:
        if not texts:
            return []

        from google.genai import types  # local import keeps top-level light

        # The SDK accepts `contents` as either a single string or a list. Passing
        # the full list is one round-trip; the API itself batches.
        try:
            response = self._client.models.embed_content(
                model=self.model,
                contents=texts,
                config=types.EmbedContentConfig(task_type=self.task_type),
            )
        except Exception as exc:
            logger.error("Gemini embed_content failed: %s", exc)
            raise

        # Response shape: response.embeddings is a list aligned with input texts.
        result: list[np.ndarray] = []
        for embedding in response.embeddings:
            values = _coerce_to_floats(embedding)
            vec = np.asarray(values, dtype=np.float32)
            # Normalize so cosine sim collapses to a dot product downstream.
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec = vec / norm
            result.append(vec)
        return result


def _coerce_to_floats(embedding: Any) -> list[float]:
    """Pull the raw float list out of a Gemini ContentEmbedding object."""
    # The SDK has shifted the field name across versions — try the common ones.
    for attr in ("values", "value", "embedding"):
        if hasattr(embedding, attr):
            v = getattr(embedding, attr)
            if v is not None:
                return list(v)
    if isinstance(embedding, dict) and "values" in embedding:
        return list(embedding["values"])
    raise RuntimeError(
        f"Could not extract float list from Gemini embedding object: {type(embedding)}"
    )
