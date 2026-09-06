"""send_ntfy: the one HTTP call the security alert's phone push depends on."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from nova.integrations.ntfy import send_ntfy


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class FakeAsyncClient:
    instances: ClassVar[list[FakeAsyncClient]] = []
    #: Class-level so a test can control the response of the *next* client a
    #: send_ntfy call constructs — each call makes its own fresh instance, so
    #: setting an attribute on an already-created one would be too late.
    next_status_code = 200

    def __init__(self, *_: Any, **__: Any) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.response = FakeResponse(FakeAsyncClient.next_status_code)
        FakeAsyncClient.instances.append(self)

    async def __aenter__(self) -> FakeAsyncClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def post(self, url: str, *, json: dict[str, Any]) -> FakeResponse:
        self.posts.append((url, json))
        return self.response


@pytest.fixture(autouse=True)
def fake_httpx(monkeypatch: pytest.MonkeyPatch) -> type[FakeAsyncClient]:
    FakeAsyncClient.instances = []
    FakeAsyncClient.next_status_code = 200
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    return FakeAsyncClient


async def test_posts_the_topic_title_and_message() -> None:
    ok = await send_ntfy(
        "https://ntfy.sh", "nova-room-abc", title="Alert", message="Someone is here"
    )

    assert ok is True
    url, payload = FakeAsyncClient.instances[0].posts[0]
    assert url == "https://ntfy.sh/"
    assert payload["topic"] == "nova-room-abc"
    assert payload["title"] == "Alert"
    assert payload["message"] == "Someone is here"


async def test_a_click_url_is_included_when_given() -> None:
    await send_ntfy(
        "https://ntfy.sh", "topic", title="t", message="m", click_url="https://example.com/x"
    )

    _, payload = FakeAsyncClient.instances[0].posts[0]
    assert payload["click"] == "https://example.com/x"


async def test_no_click_key_when_not_given() -> None:
    await send_ntfy("https://ntfy.sh", "topic", title="t", message="m")

    _, payload = FakeAsyncClient.instances[0].posts[0]
    assert "click" not in payload


async def test_trailing_slash_on_server_is_handled() -> None:
    await send_ntfy("https://ntfy.sh/", "topic", title="t", message="m")

    url, _ = FakeAsyncClient.instances[0].posts[0]
    assert url == "https://ntfy.sh/"


async def test_missing_server_or_topic_sends_nothing(fake_httpx: type[FakeAsyncClient]) -> None:
    assert await send_ntfy("", "topic", title="t", message="m") is False
    assert await send_ntfy("https://ntfy.sh", "", title="t", message="m") is False
    assert fake_httpx.instances == []  # never even tried the network


async def test_a_server_error_reports_failure() -> None:
    FakeAsyncClient.next_status_code = 500

    ok = await send_ntfy("https://ntfy.sh", "topic", title="t", message="m")

    assert ok is False


async def test_a_network_error_is_caught_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    class ExplodingClient(FakeAsyncClient):
        async def post(self, *_: Any, **__: Any) -> FakeResponse:
            raise ConnectionError("no route to host")

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", ExplodingClient)

    assert await send_ntfy("https://ntfy.sh", "topic", title="t", message="m") is False
