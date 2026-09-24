"""Push notifications via ntfy.sh (or a self-hosted instance).

Deliberately small: one HTTP POST, no SDK, no account. A topic name is the
only "credential" ntfy has — ntfy.sh itself is a public relay, so a topic
should be a long, unguessable string (see SecurityService._ensure_topic),
treated as a shared secret rather than a username.
"""

from __future__ import annotations

from ..runtime.logging import get_logger

log = get_logger(__name__)


async def send_ntfy(
    server: str,
    topic: str,
    *,
    title: str,
    message: str,
    click_url: str = "",
    priority: int = 5,
) -> bool:
    """POST one notification. Returns whether the server accepted it.

    Uses ntfy's JSON publish endpoint rather than its header-based one — the
    header form needs RFC 2047 encoding for anything outside ASCII, and an
    alert message or enrolled name is not guaranteed to be ASCII-only.
    """
    if not server or not topic:
        return False
    import httpx

    payload: dict[str, object] = {
        "topic": topic,
        "title": title,
        "message": message,
        "priority": priority,
    }
    if click_url:
        payload["click"] = click_url

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(f"{server.rstrip('/')}/", json=payload)
        if response.status_code >= 400:
            log.warning("ntfy_send_failed", status=response.status_code)
            return False
        return True
    except Exception as exc:  # noqa: BLE001 - a failed push should not crash the watcher
        log.warning("ntfy_send_failed", error=str(exc))
        return False
