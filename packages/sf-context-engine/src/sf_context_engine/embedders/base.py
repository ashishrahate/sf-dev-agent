"""Embedder abstraction.

An `Embedder` turns a list of text chunks into a list of float vectors. Each
implementation owns the model name, output dimensionality, and any provider
SDK quirks. Callers (the metadata index) treat embedders as opaque.

Adding a new provider — OpenAI, Anthropic-via-Voyage, local sentence-transformer
— means writing one Embedder subclass and adding a branch to `create_embedder()`
in `__init__.py`.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from typing import Iterable

import numpy as np


class Embedder(ABC):
    """Base class for text embedders."""

    name: str = ""           # provider+model identifier ("gemini:text-embedding-004")
    dim: int = 0             # output vector dimensionality

    @abstractmethod
    def embed(self, texts: list[str]) -> list[np.ndarray]:
        """Embed a batch of texts. Output is a list of float32 numpy vectors."""

    def embed_one(self, text: str) -> np.ndarray:
        return self.embed([text])[0]


class MockEmbedder(Embedder):
    """Deterministic hash-based embedder for tests.

    Pure Python — no API calls, no randomness. The same text always maps to the
    same vector. Different texts that share words have similar vectors (a tiny
    bag-of-words signal), enough for ranking-correctness tests.
    """

    name = "mock:hashbow"

    def __init__(self, dim: int = 64) -> None:
        self.dim = dim

    def embed(self, texts: list[str]) -> list[np.ndarray]:
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> np.ndarray:
        vec = np.zeros(self.dim, dtype=np.float32)
        # Tokenize on whitespace + alphanumeric runs; skip punctuation so
        # similarity is driven by the words present.
        for token in self._tokens(text):
            idx = int(hashlib.sha256(token.encode("utf-8")).hexdigest()[:8], 16) % self.dim
            vec[idx] += 1.0
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec /= norm
        return vec

    @staticmethod
    def _tokens(text: str) -> Iterable[str]:
        buf: list[str] = []
        for ch in text.lower():
            if ch.isalnum() or ch == "_":
                buf.append(ch)
            else:
                if buf:
                    yield "".join(buf)
                    buf = []
        if buf:
            yield "".join(buf)


def hash_text(text: str) -> str:
    """Stable SHA-256 hex digest used to gate re-embeds."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
