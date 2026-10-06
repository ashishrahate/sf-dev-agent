"""Local ONNX embedder via fastembed — no API key, no server, works offline.

Default model is pinned (not "whatever fastembed's default is this release")
so an index built today still matches queries after a package upgrade.
The model (~67 MB) downloads once from Hugging Face into fastembed's cache on
first use; `sf-context-mcp --warmup` pre-downloads it.

Env:
    SF_CONTEXT_EMBEDDER_MODEL       override the model name
    SF_CONTEXT_EMBEDDER_CACHE_DIR   model cache directory (default: fastembed's)
"""

from __future__ import annotations

import logging
import os
from typing import Any

import numpy as np

from sf_context_engine.embedders.base import Embedder

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"


class FastEmbedEmbedder(Embedder):
    def __init__(
        self,
        model: str | None = None,
        task_type: str = "RETRIEVAL_DOCUMENT",
        cache_dir: str | None = None,
    ) -> None:
        self.model = model or os.environ.get("SF_CONTEXT_EMBEDDER_MODEL") or DEFAULT_MODEL
        self.task_type = task_type
        self.name = f"fastembed:{self.model}"
        self._cache_dir = cache_dir or os.environ.get("SF_CONTEXT_EMBEDDER_CACHE_DIR") or None
        self._engine: Any = None
        self.dim = self._lookup_dim(self.model)

    @staticmethod
    def _import() -> Any:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise ImportError(
                "fastembed is not installed. Run: pip install 'sf-context-engine[local]'"
            ) from exc
        return TextEmbedding

    def _lookup_dim(self, model: str) -> int:
        for entry in self._import().list_supported_models():
            if entry["model"].lower() == model.lower():
                return int(entry["dim"])
        raise ValueError(f"Model {model!r} is not supported by fastembed")

    def _load(self) -> Any:
        """Create the ONNX session lazily; this is where the model download happens."""
        if self._engine is None:
            kwargs: dict[str, Any] = {"model_name": self.model}
            if self._cache_dir:
                kwargs["cache_dir"] = self._cache_dir
            logger.info("Loading fastembed model %s (downloads on first use)", self.model)
            self._engine = self._import()(**kwargs)
        return self._engine

    def warmup(self) -> None:
        """Download (if needed) and load the model."""
        self._load()

    def embed(self, texts: list[str]) -> list[np.ndarray]:
        if not texts:
            return []
        engine = self._load()
        # BGE models are trained with an instruction on the query side only.
        if self.task_type == "RETRIEVAL_QUERY":
            vectors = engine.query_embed(texts)
        else:
            vectors = engine.embed(texts)
        out: list[np.ndarray] = []
        for v in vectors:
            arr = np.asarray(v, dtype=np.float32)
            norm = float(np.linalg.norm(arr))
            out.append(arr / norm if norm > 0 else arr)
        return out
