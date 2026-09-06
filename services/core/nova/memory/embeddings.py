"""Local sentence embeddings for semantic recall.

Optional by design. Without ``sentence-transformers`` the memory store still
works — it falls back to SQLite FTS5, which handles "what did I say about the
NAS" perfectly well and costs nothing. The embedding model only earns its keep
for paraphrased recall ("the thing I told you about storage").

Encoding runs in a worker thread so a 30 ms model call never blocks the event
loop while N.O.V.A. is speaking.
"""

from __future__ import annotations

import asyncio
import struct
from typing import Any

from ..runtime.logging import get_logger

log = get_logger(__name__)


class Embedder:
    """Lazily-loaded local embedding model."""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self._model: Any = None
        self._dim: int = 0
        self._load_failed = False
        self._lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        return self._model is not None

    @property
    def dimensions(self) -> int:
        return self._dim

    async def load(self) -> bool:
        """Load the model. Returns False if embeddings are unavailable."""
        if self._model is not None:
            return True
        if self._load_failed:
            return False
        async with self._lock:
            if self._model is not None:
                return True
            try:
                self._model = await asyncio.to_thread(self._load_sync)
            except Exception as exc:  # noqa: BLE001 - any failure means "no embeddings"
                self._load_failed = True
                log.info("embeddings_unavailable", reason=str(exc), model=self.model_name)
                return False
            self._dim = int(self._model.get_sentence_embedding_dimension())
            log.info("embeddings_ready", model=self.model_name, dim=self._dim)
            return True

    def _load_sync(self) -> Any:
        from sentence_transformers import SentenceTransformer

        return SentenceTransformer(self.model_name)

    async def encode(self, text: str) -> bytes | None:
        if not await self.load():
            return None
        vector = await asyncio.to_thread(self._encode_sync, text)
        return vector

    def _encode_sync(self, text: str) -> bytes:
        vec = self._model.encode(text, normalize_embeddings=True, show_progress_bar=False)
        return pack_vector([float(x) for x in vec])

    async def encode_many(self, texts: list[str]) -> list[bytes] | None:
        if not texts or not await self.load():
            return None
        return await asyncio.to_thread(self._encode_many_sync, texts)

    def _encode_many_sync(self, texts: list[str]) -> list[bytes]:
        vectors = self._model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return [pack_vector([float(x) for x in v]) for v in vectors]


def pack_vector(values: list[float]) -> bytes:
    return struct.pack(f"<{len(values)}f", *values)


def unpack_vector(blob: bytes) -> list[float]:
    count = len(blob) // 4
    return list(struct.unpack(f"<{count}f", blob[: count * 4]))


def cosine(a: bytes, b: bytes) -> float:
    """Cosine similarity of two packed vectors.

    Vectors are stored normalised, so this reduces to a dot product; the
    magnitude guard only matters for vectors written by an older build.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    va, vb = unpack_vector(a), unpack_vector(b)
    dot = sum(x * y for x, y in zip(va, vb, strict=True))
    norm_a = sum(x * x for x in va) ** 0.5
    norm_b = sum(y * y for y in vb) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    if 0.99 < norm_a < 1.01 and 0.99 < norm_b < 1.01:
        return dot
    return dot / (norm_a * norm_b)
