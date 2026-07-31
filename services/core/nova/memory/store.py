"""SQLite-backed persistent memory.

Everything N.O.V.A. knows lives in one file on the user's disk. No cloud, no
vector service, no daemon. Recall is hybrid: SQLite FTS5 supplies lexical
matching (always available, very fast) and — when a local embedding model is
present — cosine similarity supplies paraphrase matching. The two are blended
with reciprocal rank fusion, which needs no score calibration between them.

The connection is opened in WAL mode and used from a single worker thread owned
by :class:`~nova.memory.service.MemoryService`, so the ``check_same_thread``
guard stays on and there is no lock contention to reason about.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..runtime.logging import get_logger
from .embeddings import cosine
from .models import Entity, Memory, MemoryKind, Turn

log = get_logger(__name__)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT    NOT NULL,
    subject      TEXT    NOT NULL DEFAULT '',
    content      TEXT    NOT NULL,
    importance   REAL    NOT NULL DEFAULT 0.5,
    source       TEXT    NOT NULL DEFAULT 'conversation',
    metadata     TEXT    NOT NULL DEFAULT '{}',
    embedding    BLOB,
    created_at   REAL    NOT NULL,
    updated_at   REAL    NOT NULL,
    accessed_at  REAL    NOT NULL DEFAULT 0,
    access_count INTEGER NOT NULL DEFAULT 0,
    expires_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_memories_kind    ON memories(kind);
CREATE INDEX IF NOT EXISTS idx_memories_subject ON memories(subject);
CREATE INDEX IF NOT EXISTS idx_memories_expiry  ON memories(expires_at);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content, subject, kind UNINDEXED, content='memories', content_rowid='id',
    tokenize='porter unicode61'
);

-- Keep the FTS index in lockstep with the base table.
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content, subject, kind)
    VALUES (new.id, new.content, new.subject, new.kind);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, subject, kind)
    VALUES ('delete', old.id, old.content, old.subject, old.kind);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, subject, kind)
    VALUES ('delete', old.id, old.content, old.subject, old.kind);
    INSERT INTO memories_fts(rowid, content, subject, kind)
    VALUES (new.id, new.content, new.subject, new.kind);
END;

CREATE TABLE IF NOT EXISTS conversations (
    id         TEXT PRIMARY KEY,
    started_at REAL NOT NULL,
    ended_at   REAL,
    summary    TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS turns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    metadata        TEXT NOT NULL DEFAULT '{}',
    created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turns_conversation ON turns(conversation_id, id);

CREATE TABLE IF NOT EXISTS entities (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    name       TEXT NOT NULL,
    aliases    TEXT NOT NULL DEFAULT '[]',
    attributes TEXT NOT NULL DEFAULT '{}',
    updated_at REAL NOT NULL,
    UNIQUE(kind, name)
);

CREATE TABLE IF NOT EXISTS preferences (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""


class MemoryStore:
    """Synchronous SQLite access. Call from one thread only."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        version = int(self._db.execute("PRAGMA user_version").fetchone()[0])
        if version == 0:
            self._db.executescript(_SCHEMA)
            self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            log.info("memory_schema_created", version=SCHEMA_VERSION, path=str(self.path))
        elif version < SCHEMA_VERSION:  # pragma: no cover - future migrations land here
            log.info("memory_schema_migrating", frm=version, to=SCHEMA_VERSION)
            self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def close(self) -> None:
        self._db.close()

    # ---------------------------------------------------------------- writing

    def remember(self, memory: Memory, embedding: bytes | None = None) -> Memory:
        """Insert a memory, or reinforce a near-duplicate instead of duplicating it."""
        existing = self._find_duplicate(memory)
        now = time.time()
        if existing is not None:
            self._db.execute(
                """UPDATE memories
                   SET importance = MIN(1.0, importance + 0.1), updated_at = ?,
                       content = ?, embedding = COALESCE(?, embedding)
                   WHERE id = ?""",
                (now, memory.content, embedding, existing),
            )
            memory.id = existing
            memory.updated_at = now
            return memory

        cursor = self._db.execute(
            """INSERT INTO memories
               (kind, subject, content, importance, source, metadata, embedding,
                created_at, updated_at, expires_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                memory.kind.value,
                memory.subject,
                memory.content,
                memory.importance,
                memory.source,
                json.dumps(memory.metadata),
                embedding,
                memory.created_at,
                now,
                memory.expires_at,
            ),
        )
        memory.id = int(cursor.lastrowid or 0)
        return memory

    def _find_duplicate(self, memory: Memory) -> int | None:
        row = self._db.execute(
            "SELECT id FROM memories WHERE kind = ? AND subject = ? AND content = ? LIMIT 1",
            (memory.kind.value, memory.subject, memory.content),
        ).fetchone()
        if row is not None:
            return int(row["id"])
        # A preference about the same subject supersedes the previous one.
        if memory.kind is MemoryKind.PREFERENCE and memory.subject:
            row = self._db.execute(
                """SELECT id FROM memories WHERE kind = ? AND subject = ?
                   ORDER BY updated_at DESC LIMIT 1""",
                (memory.kind.value, memory.subject),
            ).fetchone()
            if row is not None:
                return int(row["id"])
        return None

    def set_embedding(self, memory_id: int, embedding: bytes) -> None:
        self._db.execute("UPDATE memories SET embedding = ? WHERE id = ?", (embedding, memory_id))

    def forget(self, memory_id: int) -> bool:
        cursor = self._db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        return cursor.rowcount > 0

    def forget_matching(self, query: str, *, limit: int = 20) -> int:
        ids = [m.id for m in self.search_lexical(query, limit=limit) if m.id is not None]
        for memory_id in ids:
            self.forget(memory_id)
        return len(ids)

    def prune(self, retention_days: int = 0) -> int:
        """Drop expired rows, plus low-value ones older than the retention window."""
        now = time.time()
        removed = self._db.execute(
            "DELETE FROM memories WHERE expires_at IS NOT NULL AND expires_at < ?", (now,)
        ).rowcount
        if retention_days > 0:
            cutoff = now - retention_days * 86400
            removed += self._db.execute(
                """DELETE FROM memories
                   WHERE created_at < ? AND importance < 0.7
                     AND kind NOT IN ('preference','entity')""",
                (cutoff,),
            ).rowcount
        return removed

    # ---------------------------------------------------------------- reading

    def get(self, memory_id: int) -> Memory | None:
        row = self._db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return _row_to_memory(row) if row else None

    def all_memories(self, *, kind: MemoryKind | None = None, limit: int = 200) -> list[Memory]:
        if kind is not None:
            rows = self._db.execute(
                """SELECT * FROM memories WHERE kind = ?
                   ORDER BY importance DESC, updated_at DESC LIMIT ?""",
                (kind.value, limit),
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM memories ORDER BY importance DESC, updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_memory(r) for r in rows]

    def count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM memories").fetchone()[0])

    def memories_without_embeddings(self, limit: int = 500) -> list[Memory]:
        """Rows written before the embedding model finished loading."""
        rows = self._db.execute(
            "SELECT * FROM memories WHERE embedding IS NULL ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_row_to_memory(r) for r in rows]

    def search_lexical(self, query: str, *, limit: int = 10) -> list[Memory]:
        """FTS5 full-text search, ordered by BM25."""
        match = _to_fts_query(query)
        if not match:
            return []
        try:
            rows = self._db.execute(
                """SELECT m.*, bm25(memories_fts) AS rank
                   FROM memories_fts JOIN memories m ON m.id = memories_fts.rowid
                   WHERE memories_fts MATCH ?
                   ORDER BY rank LIMIT ?""",
                (match, limit),
            ).fetchall()
        except sqlite3.OperationalError as exc:  # malformed user text reaching FTS
            log.debug("fts_query_rejected", error=str(exc))
            return []
        results = []
        for row in rows:
            memory = _row_to_memory(row)
            # bm25 returns increasingly negative values for better matches.
            memory.score = 1.0 / (1.0 + math.exp(float(row["rank"]) / 4.0))
            results.append(memory)
        return results

    def search_semantic(
        self, embedding: bytes, *, limit: int = 10, floor: float = 0.25
    ) -> list[Memory]:
        """Brute-force cosine over stored vectors.

        Linear scan is the right call here: personal memory tops out in the low
        tens of thousands of rows, where a scan costs single-digit milliseconds
        and an ANN index would add a dependency plus a rebuild step for nothing.
        """
        rows = self._db.execute("SELECT * FROM memories WHERE embedding IS NOT NULL").fetchall()
        scored: list[Memory] = []
        for row in rows:
            similarity = cosine(embedding, row["embedding"])
            if similarity < floor:
                continue
            memory = _row_to_memory(row)
            memory.score = similarity
            scored.append(memory)
        scored.sort(key=lambda m: m.score, reverse=True)
        return scored[:limit]

    def touch(self, ids: Iterable[int]) -> None:
        now = time.time()
        self._db.executemany(
            "UPDATE memories SET accessed_at = ?, access_count = access_count + 1 WHERE id = ?",
            [(now, i) for i in ids],
        )

    # ------------------------------------------------------------ preferences

    def set_preference(self, key: str, value: Any) -> None:
        self._db.execute(
            """INSERT INTO preferences(key, value, updated_at) VALUES (?,?,?)
               ON CONFLICT(key) DO UPDATE SET
                 value = excluded.value, updated_at = excluded.updated_at""",
            (key, json.dumps(value), time.time()),
        )

    def get_preference(self, key: str, default: Any = None) -> Any:
        row = self._db.execute("SELECT value FROM preferences WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default

    def all_preferences(self) -> dict[str, Any]:
        rows = self._db.execute("SELECT key, value FROM preferences").fetchall()
        out: dict[str, Any] = {}
        for row in rows:
            try:
                out[row["key"]] = json.loads(row["value"])
            except json.JSONDecodeError:
                continue
        return out

    # --------------------------------------------------------------- entities

    def upsert_entity(self, entity: Entity) -> Entity:
        now = time.time()
        self._db.execute(
            """INSERT INTO entities(kind, name, aliases, attributes, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(kind, name) DO UPDATE SET
                 aliases = excluded.aliases,
                 attributes = json_patch(entities.attributes, excluded.attributes),
                 updated_at = excluded.updated_at""",
            (
                entity.kind,
                entity.name,
                json.dumps(entity.aliases),
                json.dumps(entity.attributes),
                now,
            ),
        )
        row = self._db.execute(
            "SELECT * FROM entities WHERE kind = ? AND name = ?", (entity.kind, entity.name)
        ).fetchone()
        return _row_to_entity(row)

    def find_entities(self, text: str, *, kind: str | None = None, limit: int = 10) -> list[Entity]:
        """Match on name or any alias, case-insensitively."""
        pattern = f"%{text.lower()}%"
        sql = """SELECT * FROM entities
                 WHERE (LOWER(name) LIKE ? OR LOWER(aliases) LIKE ?)"""
        params: list[Any] = [pattern, pattern]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        return [_row_to_entity(r) for r in self._db.execute(sql, params).fetchall()]

    def list_entities(self, kind: str | None = None, limit: int = 500) -> list[Entity]:
        if kind:
            rows = self._db.execute(
                "SELECT * FROM entities WHERE kind = ? ORDER BY name LIMIT ?", (kind, limit)
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM entities ORDER BY kind, name LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_entity(r) for r in rows]

    # ---------------------------------------------------------- conversations

    def start_conversation(self, conversation_id: str) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO conversations(id, started_at) VALUES (?,?)",
            (conversation_id, time.time()),
        )

    def add_turn(self, turn: Turn) -> Turn:
        cursor = self._db.execute(
            """INSERT INTO turns(conversation_id, role, content, metadata, created_at)
               VALUES (?,?,?,?,?)""",
            (
                turn.conversation_id,
                turn.role,
                turn.content,
                json.dumps(turn.metadata),
                turn.created_at,
            ),
        )
        turn.id = int(cursor.lastrowid or 0)
        return turn

    def recent_turns(self, conversation_id: str, limit: int = 12) -> list[Turn]:
        rows = self._db.execute(
            "SELECT * FROM turns WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
            (conversation_id, limit),
        ).fetchall()
        return [_row_to_turn(r) for r in reversed(rows)]

    def close_conversation(self, conversation_id: str, summary: str = "") -> None:
        self._db.execute(
            "UPDATE conversations SET ended_at = ?, summary = ? WHERE id = ?",
            (time.time(), summary, conversation_id),
        )

    def stats(self) -> dict[str, Any]:
        def scalar(sql: str) -> int:
            return int(self._db.execute(sql).fetchone()[0])

        return {
            "memories": scalar("SELECT COUNT(*) FROM memories"),
            "embedded": scalar("SELECT COUNT(*) FROM memories WHERE embedding IS NOT NULL"),
            "entities": scalar("SELECT COUNT(*) FROM entities"),
            "turns": scalar("SELECT COUNT(*) FROM turns"),
            "preferences": scalar("SELECT COUNT(*) FROM preferences"),
            "sizeBytes": self.path.stat().st_size if self.path.exists() else 0,
        }


# ------------------------------------------------------------------- mapping


def _row_to_memory(row: sqlite3.Row) -> Memory:
    return Memory(
        id=int(row["id"]),
        kind=MemoryKind(row["kind"]),
        subject=row["subject"],
        content=row["content"],
        importance=float(row["importance"]),
        source=row["source"],
        metadata=_loads(row["metadata"], {}),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        accessed_at=float(row["accessed_at"]),
        access_count=int(row["access_count"]),
        expires_at=row["expires_at"],
    )


def _row_to_entity(row: sqlite3.Row) -> Entity:
    return Entity(
        id=int(row["id"]),
        kind=row["kind"],
        name=row["name"],
        aliases=_loads(row["aliases"], []),
        attributes=_loads(row["attributes"], {}),
        updated_at=float(row["updated_at"]),
    )


def _row_to_turn(row: sqlite3.Row) -> Turn:
    return Turn(
        id=int(row["id"]),
        conversation_id=row["conversation_id"],
        role=row["role"],
        content=row["content"],
        metadata=_loads(row["metadata"], {}),
        created_at=float(row["created_at"]),
    )


def _loads(raw: str, fallback: Any) -> Any:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return fallback


def _to_fts_query(query: str) -> str:
    """Turn free-form speech into a safe FTS5 MATCH expression.

    Every token is quoted, so punctuation and FTS operators in a transcript
    ("what's the NAS's IP?") can't produce a syntax error. ``OR`` beats ``AND``
    here because recall should degrade gracefully rather than return nothing.
    """
    tokens = [t for t in "".join(c if c.isalnum() else " " for c in query).split() if len(t) > 1]
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens[:16])


def reciprocal_rank_fusion(*rankings: list[Memory], k: int = 60, limit: int = 10) -> list[Memory]:
    """Blend ranked lists without needing their scores to be comparable.

    RRF only looks at position, which is exactly what we want when combining a
    BM25 ranking with a cosine ranking — no normalisation, no tuning constant
    per corpus.
    """
    scores: dict[int, float] = {}
    seen: dict[int, Memory] = {}
    for ranking in rankings:
        for position, memory in enumerate(ranking):
            if memory.id is None:
                continue
            scores[memory.id] = scores.get(memory.id, 0.0) + 1.0 / (k + position + 1)
            seen.setdefault(memory.id, memory)
    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    output: list[Memory] = []
    for memory_id, score in ordered:
        memory = seen[memory_id]
        memory.score = score
        output.append(memory)
    return output
