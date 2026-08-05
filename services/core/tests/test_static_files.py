"""Static file resolution for the mobile web client.

Runs on every plain HTTP GET the bridge receives — the same port a phone's
browser loads the app shell from before it ever opens a WebSocket — so a path
that escapes the static root here is a real read-anything-on-disk bug, not
just a broken link.
"""

from __future__ import annotations

from pathlib import Path

from nova.transport.server import resolve_static_path


def make_root(tmp_path: Path) -> Path:
    root = tmp_path / "static"
    root.mkdir()
    (root / "index.html").write_text("<html>shell</html>")
    (root / "app.js").write_text("console.log('hi')")
    sub = root / "icons"
    sub.mkdir()
    (sub / "icon.svg").write_text("<svg></svg>")
    return root


def test_root_path_serves_index(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    assert resolve_static_path(root, "/") == root / "index.html"


def test_a_real_file_resolves_directly(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    assert resolve_static_path(root, "/app.js") == root / "app.js"


def test_a_query_string_is_ignored(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    assert resolve_static_path(root, "/app.js?v=2") == root / "app.js"
    assert resolve_static_path(root, "/?token=abc123") == root / "index.html"


def test_a_nested_real_file_resolves(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    assert resolve_static_path(root, "/icons/icon.svg") == root / "icons" / "icon.svg"


def test_an_extensionless_path_falls_back_to_index(tmp_path: Path) -> None:
    """No client-side router to serve, but a bookmarked/typo'd path should
    still land on the app shell rather than a bare 404."""
    root = make_root(tmp_path)
    assert resolve_static_path(root, "/settings") == root / "index.html"


def test_a_missing_file_resolves_to_nothing(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    assert resolve_static_path(root, "/nonexistent.js") is None


def test_directory_traversal_is_blocked(tmp_path: Path) -> None:
    """The real invariant: whatever comes back, it is never a path outside
    `root` — whether that lands as None or falls back to index.html depends
    on the attempt, but a file from outside the static directory must never
    be the answer."""
    root = make_root(tmp_path)
    (tmp_path / "secret.txt").write_text("token=super-secret")

    for attempt in ("/../secret.txt", "/../../etc/passwd", "/%2e%2e/secret.txt"):
        result = resolve_static_path(root, attempt)
        assert result is None or root.resolve() in (result.resolve(), *result.resolve().parents)


def test_missing_static_root_does_not_crash(tmp_path: Path) -> None:
    assert resolve_static_path(tmp_path / "does-not-exist", "/") is None
