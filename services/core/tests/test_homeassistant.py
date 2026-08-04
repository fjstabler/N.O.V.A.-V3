"""Home Assistant client: entity resolution and the service-call guard.

Regression covered here: a "turn it on" command reached ``call_service`` with
an entity id the model invented from mis-heard speech ("ceiling light" heard
as "Sealing Light" became ``light.sealing_light``). Home Assistant's service
API does not error on an id matching nothing — it just no-ops and returns
200 — so N.O.V.A. logged success and told the user it was done while nothing
in the house moved.
"""

from __future__ import annotations

from typing import Any

import pytest

from nova.integrations.homeassistant import HAEntity, HomeAssistantClient
from nova.runtime.errors import IntegrationError


def make_client(**entities: HAEntity) -> HomeAssistantClient:
    client = HomeAssistantClient("http://ha.local:8123", "token")
    client._entities.update(entities)
    return client


def stub_post(client: HomeAssistantClient, calls: list[dict[str, Any]]) -> None:
    async def _post(path: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        calls.append({"path": path, "payload": payload})
        return []

    client._post = _post  # type: ignore[method-assign]


ceiling = HAEntity(
    entity_id="light.ceiling_light", state="off", attributes={"friendly_name": "Ceiling Light"}
)


async def test_a_real_entity_id_passes_through_untouched() -> None:
    client = make_client(**{ceiling.entity_id: ceiling})
    calls: list[dict[str, Any]] = []
    stub_post(client, calls)

    await client.call_service("light", "turn_on", "light.ceiling_light")

    assert calls[0]["payload"]["entity_id"] == "light.ceiling_light"


async def test_a_friendly_name_used_as_an_id_is_resolved_to_the_real_one() -> None:
    """A model that does not know Home Assistant's id convention sometimes
    passes the friendly name straight through as "entity_id" — that still
    has to land on the right device rather than nothing at all."""
    client = make_client(**{ceiling.entity_id: ceiling})
    calls: list[dict[str, Any]] = []
    stub_post(client, calls)

    await client.call_service("light", "turn_on", "Ceiling Light")

    assert calls[0]["payload"]["entity_id"] == "light.ceiling_light"


async def test_an_id_matching_nothing_fails_loudly_instead_of_silently_doing_nothing() -> None:
    client = make_client(**{ceiling.entity_id: ceiling})
    calls: list[dict[str, Any]] = []
    stub_post(client, calls)

    with pytest.raises(IntegrationError, match="can't find anything called"):
        await client.call_service("light", "turn_on", "light.attic_fan")

    assert calls == []  # never reached Home Assistant with the bad id


async def test_a_call_with_no_entity_id_is_unaffected() -> None:
    """Some services (e.g. triggering an automation) take no target at all."""
    client = make_client(**{ceiling.entity_id: ceiling})
    calls: list[dict[str, Any]] = []
    stub_post(client, calls)

    await client.call_service("homeassistant", "reload_config_entry")

    assert calls[0]["payload"] == {}
