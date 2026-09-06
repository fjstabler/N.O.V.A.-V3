"""The bridge's `/camera/<slug>` route: token gate and source dispatch.

`display.show_camera` never sends image bytes itself — it publishes a
`streamPath` and the frontend polls this route for a fresh JPEG each time.
That split means the route has to enforce the same token gate a WebSocket
connection would, and correctly tell a locally-attached camera (`local:name`,
served via OpenCV) from a Home Assistant one (`ha:entity_id`, served via a
snapshot proxy call) apart.
"""

from __future__ import annotations

from typing import Any

import pytest

from nova.context import NovaContext
from nova.integrations.homeassistant import HomeAssistantClient
from nova.integrations.services import HomeService
from nova.runtime.service import ServiceState
from nova.transport.router import RequestRouter
from nova.transport.server import BridgeService


class FakeRequest:
    def __init__(self, path: str, headers: dict[str, str] | None = None) -> None:
        self.path = path
        self.headers = headers or {}


def make_bridge(ctx: NovaContext) -> BridgeService:
    return BridgeService(ctx, RequestRouter())


# ------------------------------------------------------------------- auth


async def test_no_token_configured_allows_the_request(ctx: NovaContext) -> None:
    ctx.store.patch({"transport": {"token": ""}}, persist=False)
    bridge = make_bridge(ctx)
    assert bridge._request_authorised(FakeRequest("/camera/local:bedroom")) is True


async def test_correct_query_token_is_authorised(ctx: NovaContext) -> None:
    ctx.store.patch({"transport": {"token": "secret"}}, persist=False)
    bridge = make_bridge(ctx)
    assert bridge._request_authorised(FakeRequest("/camera/local:bedroom?token=secret")) is True


async def test_wrong_or_missing_token_is_refused(ctx: NovaContext) -> None:
    ctx.store.patch({"transport": {"token": "secret"}}, persist=False)
    bridge = make_bridge(ctx)
    assert bridge._request_authorised(FakeRequest("/camera/local:bedroom?token=wrong")) is False
    assert bridge._request_authorised(FakeRequest("/camera/local:bedroom")) is False


async def test_bearer_header_token_is_authorised(ctx: NovaContext) -> None:
    ctx.store.patch({"transport": {"token": "secret"}}, persist=False)
    bridge = make_bridge(ctx)
    request = FakeRequest("/camera/local:bedroom", {"Authorization": "Bearer secret"})
    assert bridge._request_authorised(request) is True


async def test_unauthorised_request_never_reaches_a_camera_source(ctx: NovaContext) -> None:
    """The 401 has to come back before any capture is attempted — a picture
    should never be the side effect of a rejected request."""
    ctx.store.patch({"transport": {"token": "secret"}}, persist=False)
    ctx.store.patch({"vision": {"named_cameras": [{"name": "bedroom", "index": 0}]}}, persist=False)
    bridge = make_bridge(ctx)

    response = await bridge._camera_response(
        FakeRequest("/camera/local:bedroom"), "/camera/local:bedroom"
    )

    assert response.status_code == 401


# --------------------------------------------------------------- dispatch


async def test_an_unresolvable_slug_is_a_404(ctx: NovaContext) -> None:
    ctx.store.patch({"transport": {"token": ""}}, persist=False)
    bridge = make_bridge(ctx)

    response = await bridge._camera_response(
        FakeRequest("/camera/local:nonexistent"), "/camera/local:nonexistent"
    )

    assert response.status_code == 404


async def test_a_local_camera_is_captured_by_configured_index(
    ctx: NovaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.store.patch({"transport": {"token": ""}}, persist=False)
    ctx.store.patch({"vision": {"named_cameras": [{"name": "bedroom", "index": 3}]}}, persist=False)
    bridge = make_bridge(ctx)

    captured: list[int] = []

    async def fake_snapshot(index: int, **_: Any) -> bytes:
        captured.append(index)
        return b"\xff\xd8fake-jpeg"

    monkeypatch.setattr("nova.transport.server.local_camera_pool.snapshot_jpeg", fake_snapshot)

    response = await bridge._camera_response(
        FakeRequest("/camera/local:bedroom"), "/camera/local:bedroom"
    )

    assert response.status_code == 200
    assert response.body == b"\xff\xd8fake-jpeg"
    assert captured == [3]


async def test_a_home_assistant_camera_is_proxied_by_entity_id(ctx: NovaContext) -> None:
    ctx.store.patch({"transport": {"token": ""}}, persist=False)
    bridge = make_bridge(ctx)

    home = HomeService(ctx)
    ctx.services.register(home)
    client = HomeAssistantClient("http://ha.local:8123", "token")

    async def fake_snapshot(entity_id: str) -> bytes | None:
        assert entity_id == "camera.front_door"
        return b"\xff\xd8ring-frame"

    client.camera_snapshot_jpeg = fake_snapshot  # type: ignore[method-assign]
    home.ha = client
    home._set_state(ServiceState.RUNNING)

    response = await bridge._camera_response(
        FakeRequest("/camera/ha:camera.front_door"), "/camera/ha:camera.front_door"
    )

    assert response.status_code == 200
    assert response.body == b"\xff\xd8ring-frame"


async def test_home_assistant_not_connected_is_a_404_not_a_crash(ctx: NovaContext) -> None:
    ctx.store.patch({"transport": {"token": ""}}, persist=False)
    bridge = make_bridge(ctx)

    response = await bridge._camera_response(
        FakeRequest("/camera/ha:camera.front_door"), "/camera/ha:camera.front_door"
    )

    assert response.status_code == 404
