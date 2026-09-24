"""Memory as a service: async facade over the SQLite store.

All database work is marshalled onto a single worker thread. That keeps SQLite's
threading contract satisfied without a lock, and keeps the event loop free — a
semantic recall over a few thousand rows costs a few milliseconds, but the loop
is also driving a 60 FPS renderer and an audio stream, so it never runs inline.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

from ..context import NovaContext
from ..runtime import Service
from .embeddings import Embedder
from .models import Entity, Memory, MemoryKind, Turn
from .store import MemoryStore, reciprocal_rank_fusion

T = TypeVar("T")


class MemoryService(Service):
    name = "memory"

    def __init__(self, ctx: NovaContext) -> None:
        super().__init__(ctx)
        self._store: MemoryStore | None = None
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nova-memory")
        self._embedder: Embedder | None = None
        self.conversation_id: str = uuid.uuid4().hex

    # -------------------------------------------------------------- lifecycle

    async def on_start(self) -> None:
        settings = self.ctx.settings.memory
        self._store = await self._run(lambda: MemoryStore(self.ctx.paths.memory_db))
        await self._run(lambda: self._store.start_conversation(self.conversation_id))  # type: ignore[union-attr]

        removed = await self._run(lambda: self._store.prune(settings.retention_days))  # type: ignore[union-attr]
        if removed:
            self.log.info("memory_pruned", removed=removed)

        if settings.semantic_recall:
            self._embedder = Embedder(settings.embedding_model)
            # Load in the background: a cold model can take seconds, and memory
            # must be usable (lexically) the instant the assistant boots.
            self.spawn(self._warm_embedder(), name="memory-embedder-warmup")

        self.log.info("memory_ready", **await self.stats())

    async def _warm_embedder(self) -> None:
        assert self._embedder is not None
        if await self._embedder.load():
            await self._backfill_embeddings()

    async def on_stop(self) -> None:
        if self._store is not None:
            store = self._store
            with contextlib.suppress(Exception):
                await self._run(lambda: store.close_conversation(self.conversation_id))
                await self._run(store.close)
        self._pool.shutdown(wait=False, cancel_futures=True)

    def describe(self) -> str:
        mode = "semantic+lexical" if self.semantic_available else "lexical"
        return f"{mode}"

    @property
    def semantic_available(self) -> bool:
        return self._embedder is not None and self._embedder.available

    async def _run(self, fn: Callable[[], T]) -> T:
        return await asyncio.get_running_loop().run_in_executor(self._pool, fn)

    @property
    def store(self) -> MemoryStore:
        if self._store is None:
            raise RuntimeError("memory store not started")
        return self._store

    # ----------------------------------------------------------------- recall

    async def remember(
        self,
        content: str,
        *,
        kind: MemoryKind = MemoryKind.FACT,
        subject: str = "",
        importance: float = 0.5,
        source: str = "conversation",
        metadata: dict[str, Any] | None = None,
        ttl_seconds: float | None = None,
    ) -> Memory:
        memory = Memory(
            content=content.strip(),
            kind=kind,
            subject=subject.strip(),
            importance=max(0.0, min(1.0, importance)),
            source=source,
            metadata=metadata or {},
            expires_at=time.time() + ttl_seconds if ttl_seconds else None,
        )
        embedding = await self._embed(memory.to_prompt_line())
        stored = await self._run(lambda: self.store.remember(memory, embedding))
        self.log.debug("memory_stored", kind=kind.value, subject=subject, id=stored.id)
        return stored

    async def recall(self, query: str, *, limit: int | None = None) -> list[Memory]:
        """Hybrid retrieval: lexical + semantic, fused by rank."""
        if not query.strip():
            return []
        limit = limit or self.ctx.settings.memory.max_recalled
        if limit <= 0:
            return []

        lexical = await self._run(lambda: self.store.search_lexical(query, limit=limit * 2))
        embedding = await self._embed(query)
        if embedding is None:
            results = lexical[:limit]
        else:
            semantic = await self._run(
                lambda: self.store.search_semantic(embedding, limit=limit * 2)
            )
            results = reciprocal_rank_fusion(lexical, semantic, limit=limit)

        ids = [m.id for m in results if m.id is not None]
        if ids:
            await self._run(lambda: self.store.touch(ids))
        return results

    async def _embed(self, text: str) -> bytes | None:
        if self._embedder is None:
            return None
        return await self._embedder.encode(text)

    async def _backfill_embeddings(self) -> None:
        """Vectorise memories written before the model finished loading.

        Also covers the first run after a user enables semantic recall, where the
        whole existing store needs indexing.
        """
        assert self._embedder is not None
        pending = await self._run(lambda: self.store.memories_without_embeddings(500))
        if not pending:
            return
        vectors = await self._embedder.encode_many([m.to_prompt_line() for m in pending])
        if vectors is None:
            return
        for memory, vector in zip(pending, vectors, strict=True):
            if memory.id is None:
                continue
            await self._run(lambda mid=memory.id, blob=vector: self.store.set_embedding(mid, blob))
        self.log.info("memory_embeddings_backfilled", count=len(pending))

    async def forget(self, memory_id: int) -> bool:
        return await self._run(lambda: self.store.forget(memory_id))

    async def forget_matching(self, query: str) -> int:
        return await self._run(lambda: self.store.forget_matching(query))

    async def list_memories(self, kind: MemoryKind | None = None, limit: int = 100) -> list[Memory]:
        return await self._run(lambda: self.store.all_memories(kind=kind, limit=limit))

    # ------------------------------------------------------------ preferences

    async def set_preference(self, key: str, value: Any) -> None:
        await self._run(lambda: self.store.set_preference(key, value))

    async def get_preference(self, key: str, default: Any = None) -> Any:
        return await self._run(lambda: self.store.get_preference(key, default))

    async def all_preferences(self) -> dict[str, Any]:
        return await self._run(self.store.all_preferences)

    # --------------------------------------------------------------- entities

    async def upsert_entity(self, entity: Entity) -> Entity:
        return await self._run(lambda: self.store.upsert_entity(entity))

    async def find_entities(self, text: str, *, kind: str | None = None) -> list[Entity]:
        return await self._run(lambda: self.store.find_entities(text, kind=kind))

    async def list_entities(self, kind: str | None = None) -> list[Entity]:
        return await self._run(lambda: self.store.list_entities(kind))

    # ---------------------------------------------------------- conversations

    async def record_turn(self, role: str, content: str, **metadata: Any) -> Turn:
        turn = Turn(
            role=role, content=content, conversation_id=self.conversation_id, metadata=metadata
        )
        return await self._run(lambda: self.store.add_turn(turn))

    async def working_context(self) -> list[Turn]:
        limit = self.ctx.settings.memory.conversation_turns
        if limit <= 0:
            return []
        return await self._run(lambda: self.store.recent_turns(self.conversation_id, limit))

    def new_conversation(self) -> str:
        """Start a fresh thread — the UI shows no history, so context is time-boxed."""
        self.conversation_id = uuid.uuid4().hex
        self.spawn(self._run(lambda: self.store.start_conversation(self.conversation_id)))
        return self.conversation_id

    async def stats(self) -> dict[str, Any]:
        base = await self._run(self.store.stats)
        base["semantic"] = self.semantic_available
        return base
