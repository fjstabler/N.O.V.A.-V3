"""Memory store: persistence, recall, entities and preferences."""

from __future__ import annotations

import time
from pathlib import Path

from nova.memory.embeddings import cosine, pack_vector
from nova.memory.models import Entity, Memory, MemoryKind, Turn
from nova.memory.store import MemoryStore, reciprocal_rank_fusion


def make_store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path / "memory.db")


def test_remembering_and_reading_back(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    stored = store.remember(Memory(content="The NAS lives in the garage", subject="NAS"))
    assert stored.id is not None
    assert store.get(stored.id).content == "The NAS lives in the garage"


def test_lexical_search_finds_by_keyword(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.remember(Memory(content="The garage NAS has eight terabytes of storage", subject="NAS"))
    store.remember(Memory(content="The kitchen lights are Hue bulbs", subject="lighting"))

    results = store.search_lexical("storage")
    assert len(results) == 1
    assert "terabytes" in results[0].content


def test_search_survives_punctuation_from_a_transcript(tmp_path: Path) -> None:
    """FTS5 operators in speech must not produce a syntax error."""
    store = make_store(tmp_path)
    store.remember(Memory(content="The NAS address is 192.168.1.10"))
    assert store.search_lexical("what's the NAS's address?") != []
    assert store.search_lexical("AND OR NOT ((") == []


def test_identical_memories_are_reinforced_not_duplicated(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    first = store.remember(Memory(content="Alice prefers warm lighting", subject="Alice"))
    second = store.remember(Memory(content="Alice prefers warm lighting", subject="Alice"))

    assert first.id == second.id
    assert store.count() == 1
    assert store.get(first.id).importance > 0.5  # reinforced


def test_a_new_preference_supersedes_the_old_one(tmp_path: Path) -> None:
    """Otherwise the model reads two contradictory preferences and picks one."""
    store = make_store(tmp_path)
    store.remember(Memory(content="Wake me at 7am", subject="alarm", kind=MemoryKind.PREFERENCE))
    store.remember(Memory(content="Wake me at 6:30am", subject="alarm", kind=MemoryKind.PREFERENCE))
    memories = store.all_memories(kind=MemoryKind.PREFERENCE)
    assert len(memories) == 1
    assert "6:30" in memories[0].content


def test_forgetting_removes_from_the_search_index_too(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    stored = store.remember(Memory(content="A secret about the boiler", subject="boiler"))
    assert store.search_lexical("boiler")
    store.forget(stored.id)
    assert store.search_lexical("boiler") == []


def test_expired_memories_are_pruned(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.remember(Memory(content="Temporary note", expires_at=time.time() - 10))
    store.remember(Memory(content="Permanent note"))
    assert store.prune() == 1
    assert store.count() == 1


def test_retention_keeps_preferences_regardless_of_age(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    old = time.time() - 400 * 86400
    store.remember(Memory(content="Trivial chatter", importance=0.2, created_at=old))
    store.remember(
        Memory(
            content="Prefers metric units",
            kind=MemoryKind.PREFERENCE,
            importance=0.2,
            created_at=old,
        )
    )
    store.prune(retention_days=30)
    remaining = [m.content for m in store.all_memories()]
    assert "Prefers metric units" in remaining
    assert "Trivial chatter" not in remaining


def test_preferences_round_trip_json_values(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.set_preference("rooms", ["kitchen", "study"])
    store.set_preference("volume", 0.8)
    assert store.get_preference("rooms") == ["kitchen", "study"]
    assert store.get_preference("volume") == 0.8
    assert store.get_preference("missing", "fallback") == "fallback"


def test_entities_are_found_by_name_or_alias(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.upsert_entity(
        Entity(kind="device", name="Kitchen Light", aliases=["cooker light", "kitchen lamp"])
    )
    assert store.find_entities("kitchen")[0].name == "Kitchen Light"
    assert store.find_entities("cooker")[0].name == "Kitchen Light"
    assert store.find_entities("bathroom") == []


def test_entity_upsert_merges_attributes(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.upsert_entity(Entity(kind="host", name="nas", attributes={"ip": "10.0.0.5"}))
    merged = store.upsert_entity(Entity(kind="host", name="nas", attributes={"os": "TrueNAS"}))
    assert merged.attributes == {"ip": "10.0.0.5", "os": "TrueNAS"}


def test_entity_upsert_accumulates_aliases_instead_of_overwriting(tmp_path: Path) -> None:
    """A second taught alias must not erase the first, and a routine re-index
    of live state (which always resupplies the same one or two aliases) must
    not undo an alias a person taught in between."""
    store = make_store(tmp_path)
    store.upsert_entity(Entity(kind="ha_entity", name="Ceiling Light", aliases=["light.ceiling"]))
    store.upsert_entity(Entity(kind="ha_entity", name="Ceiling Light", aliases=["the lamp"]))
    merged = store.upsert_entity(
        Entity(kind="ha_entity", name="Ceiling Light", aliases=["light.ceiling"])
    )

    assert merged.aliases == ["light.ceiling", "the lamp"]


def test_conversation_turns_come_back_in_order(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.start_conversation("conv-1")
    for index in range(5):
        store.add_turn(Turn(role="user", content=f"message {index}", conversation_id="conv-1"))

    turns = store.recent_turns("conv-1", limit=3)
    assert [t.content for t in turns] == ["message 2", "message 3", "message 4"]


def test_turns_are_scoped_to_their_conversation(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.add_turn(Turn(role="user", content="in one", conversation_id="a"))
    store.add_turn(Turn(role="user", content="in two", conversation_id="b"))
    assert [t.content for t in store.recent_turns("a")] == ["in one"]


def test_semantic_search_orders_by_similarity(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    near = pack_vector([1.0, 0.0, 0.0])
    far = pack_vector([0.0, 1.0, 0.0])
    store.remember(Memory(content="close match"), embedding=near)
    store.remember(Memory(content="distant match"), embedding=far)

    results = store.search_semantic(pack_vector([0.95, 0.05, 0.0]), floor=0.0)
    assert results[0].content == "close match"
    assert results[0].score > results[1].score


def test_cosine_of_normalised_vectors() -> None:
    a = pack_vector([1.0, 0.0])
    b = pack_vector([0.0, 1.0])
    assert cosine(a, a) == 1.0
    assert cosine(a, b) == 0.0
    assert cosine(a, b"") == 0.0


def test_rank_fusion_rewards_agreement_between_rankings() -> None:
    """A result both searches like should beat one only a single search found."""
    shared = Memory(content="both agree", id=1)
    lexical_only = Memory(content="lexical only", id=2)
    semantic_only = Memory(content="semantic only", id=3)

    fused = reciprocal_rank_fusion(
        [lexical_only, shared],
        [semantic_only, shared],
        limit=3,
    )
    assert fused[0].id == 1


def test_stats_reports_what_is_stored(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.remember(Memory(content="a fact"))
    store.upsert_entity(Entity(kind="room", name="study"))
    store.set_preference("units", "metric")

    stats = store.stats()
    assert stats["memories"] == 1
    assert stats["entities"] == 1
    assert stats["preferences"] == 1
    assert stats["sizeBytes"] > 0
