"""FaceStore: matching an embedding against enrolled faces, and FaceEngine's
degrade-without-opencv path.

The matching logic is pure numpy — no camera, no ONNX model, no cv2 — so it
is tested directly rather than through a real detection pipeline.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

from nova.runtime.errors import MissingDependency
from nova.security.faces import FaceEngine, FaceStore

FIN = [1.0, 0.0, 0.0, 0.0]
CLOSE_TO_FIN = [0.98, 0.05, 0.0, 0.0]  # a slightly different photo of the same face
STRANGER = [0.0, 1.0, 0.0, 0.0]


def test_an_enrolled_face_matches_itself(tmp_path: Path) -> None:
    store = FaceStore(tmp_path / "faces.json")
    store.add("Fin", FIN)

    match = store.match(FIN, threshold=0.363)

    assert match is not None
    name, score = match
    assert name == "Fin"
    assert score == pytest.approx(1.0)


def test_a_similar_but_different_embedding_still_matches(tmp_path: Path) -> None:
    """The point of a threshold rather than exact equality: the same person's
    face looks a little different photo to photo."""
    store = FaceStore(tmp_path / "faces.json")
    store.add("Fin", FIN)

    match = store.match(CLOSE_TO_FIN, threshold=0.363)

    assert match is not None
    assert match[0] == "Fin"


def test_an_unrelated_face_does_not_match(tmp_path: Path) -> None:
    store = FaceStore(tmp_path / "faces.json")
    store.add("Fin", FIN)

    assert store.match(STRANGER, threshold=0.363) is None


def test_no_enrolled_faces_never_matches(tmp_path: Path) -> None:
    store = FaceStore(tmp_path / "faces.json")
    assert store.match(FIN, threshold=0.363) is None


def test_multiple_embeddings_per_name_all_count_toward_a_match(tmp_path: Path) -> None:
    """learn_face can be called more than once — a second angle or lighting
    condition should widen recognition, not replace what was already known."""
    store = FaceStore(tmp_path / "faces.json")
    store.add("Fin", FIN)
    store.add("Fin", STRANGER)  # a second, very different reference photo

    assert store.match(FIN, threshold=0.363)[0] == "Fin"
    assert store.match(STRANGER, threshold=0.363)[0] == "Fin"


def test_the_best_matching_name_wins_when_more_than_one_is_close(tmp_path: Path) -> None:
    store = FaceStore(tmp_path / "faces.json")
    store.add("Fin", FIN)
    store.add("Housemate", [0.9, 0.3, 0.0, 0.0])  # also somewhat close to FIN

    match = store.match(FIN, threshold=0.363)

    assert match[0] == "Fin"  # the closer of the two


def test_names_lists_everyone_enrolled(tmp_path: Path) -> None:
    store = FaceStore(tmp_path / "faces.json")
    store.add("Fin", FIN)
    store.add("Housemate", STRANGER)

    assert store.names() == ["Fin", "Housemate"]


def test_forget_removes_a_name(tmp_path: Path) -> None:
    store = FaceStore(tmp_path / "faces.json")
    store.add("Fin", FIN)

    assert store.forget("Fin") is True
    assert store.names() == []
    assert store.forget("Fin") is False  # already gone


def test_persists_across_a_fresh_store_instance(tmp_path: Path) -> None:
    """Regression-shaped: add() must actually write to disk, not just update
    an in-memory copy — a fresh FaceStore pointed at the same path (e.g.
    after a restart) has to see what was enrolled before."""
    path = tmp_path / "faces.json"
    FaceStore(path).add("Fin", FIN)

    reloaded = FaceStore(path)
    assert reloaded.names() == ["Fin"]
    assert reloaded.match(FIN, threshold=0.363)[0] == "Fin"


def test_a_corrupted_file_is_treated_as_empty_not_a_crash(tmp_path: Path) -> None:
    path = tmp_path / "faces.json"
    path.write_text("{not valid json", encoding="utf-8")

    store = FaceStore(path)

    assert store.names() == []
    assert store.match(FIN, threshold=0.363) is None


def test_embeddings_survive_a_numpy_array_round_trip(tmp_path: Path) -> None:
    """add() is called with whatever SFace hands back — a numpy array, not a
    plain list — and it has to serialise to JSON regardless."""
    store = FaceStore(tmp_path / "faces.json")
    store.add("Fin", np.array([[1.0, 0.0, 0.0, 0.0]], dtype=np.float32))

    match = store.match(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32), threshold=0.363)

    assert match is not None
    assert match[0] == "Fin"


# ------------------------------------------------------------------- engine


def test_face_engine_reports_not_loaded_before_load() -> None:
    engine = FaceEngine(Path("/nonexistent"))
    assert engine.loaded is False
    assert engine.observe("any-frame") == []  # degrades to no faces found, not a crash


def test_face_engine_load_without_opencv_raises_a_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real-world equivalent of a machine that never ran
    `pip install nova-core[vision]` — forced deterministically here (via
    sys.modules) rather than relying on this environment happening to lack
    cv2 too, which would silently stop testing the right thing the moment
    it is installed for some other test."""
    monkeypatch.setitem(sys.modules, "cv2", None)
    engine = FaceEngine(tmp_path)
    with pytest.raises(MissingDependency):
        engine.load()
