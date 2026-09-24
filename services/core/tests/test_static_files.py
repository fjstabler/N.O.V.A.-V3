"""Static file resolution for the two web clients.

Runs on every plain HTTP GET the bridge receives — the same port a phone's
browser loads the app shell from before it ever opens a WebSocket — so a path
that escapes the static root here is a real read-anything-on-disk bug, not
just a broken link.

One port serves both the lightweight phone client at `/` and the full
interface at `/app/`, so which root answers a request is a decision worth
pinning down as well.
"""

from __future__ import annotations

from pathlib import Path

from websockets.datastructures import Headers

from nova.transport.server import (
    _APP_CSP,
    _CSP,
    APP_ROOT,
    STATIC_ROOT,
    _add_security_headers,
    resolve_static_path,
    route_static,
)


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


def test_security_headers_are_applied() -> None:
    """This page is served over the LAN/tailnet — unlike the Electron shell,
    which loads only its own bundled code, it's genuinely reachable by
    something other than the intended user, so it gets a real CSP."""
    headers = Headers()
    _add_security_headers(headers)

    csp = headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"


def test_the_app_policy_loosens_styles_but_never_scripts() -> None:
    """React themes the Core through inline style attributes, so `/app/` cannot
    use the phone client's stricter style policy. The line that actually keeps
    an injected payload from running is script-src, and that one does not move."""
    assert "style-src 'self' 'unsafe-inline'" in _APP_CSP
    assert "script-src 'self';" in _APP_CSP
    assert "'unsafe-inline'" not in _APP_CSP.split("script-src")[1].split(";")[0]


# ------------------------------------------------------------------ routing


def test_the_app_prefix_is_stripped_before_the_file_is_looked_up() -> None:
    """The one thing this routing has to get right: `/app/assets/x.js` names a
    file called `assets/x.js`, not one called `app/assets/x.js`."""
    root, path, _ = route_static("/app/assets/index-abc.js")

    assert root == APP_ROOT
    assert path == "/assets/index-abc.js"


def test_the_bare_prefix_still_reaches_the_app_shell() -> None:
    """`/app` without a trailing slash is what someone actually types."""
    root, path, _ = route_static("/app")

    assert root == APP_ROOT
    assert path == "/"


def test_everything_else_stays_with_the_phone_client() -> None:
    assert route_static("/")[0] == STATIC_ROOT
    assert route_static("/app.js")[0] == STATIC_ROOT
    assert route_static("/icons/icon.svg")[0] == STATIC_ROOT


def test_a_path_that_merely_starts_with_the_prefix_is_not_the_app() -> None:
    """`/application.js` shares five characters with `/app` and nothing else.
    Matching on the prefix alone would hand it to the wrong root, where it
    would 404 — confusingly, since the file exists."""
    assert route_static("/application.js")[0] == STATIC_ROOT
    assert route_static("/appearance/theme.css")[0] == STATIC_ROOT


def test_each_root_is_served_under_its_own_policy() -> None:
    assert route_static("/app/")[2] == _APP_CSP
    assert route_static("/")[2] == _CSP
