"""Editing an integration's settings must restart it, not wait for a reboot."""

from __future__ import annotations

import asyncio

from nova.app import NovaApplication
from nova.runtime.service import ServiceState


async def test_configuring_an_integration_restarts_it(store) -> None:
    app = NovaApplication(store)
    home = app.ctx.services.get("home")

    # Unconfigured: degraded, exactly as an untouched integration looks.
    await home.start()
    assert home.health.state is ServiceState.DEGRADED

    restarted = asyncio.Event()
    original = app.ctx.services.restart

    async def spy(name: str):
        try:
            return await original(name)
        finally:
            if name == "home":
                restarted.set()

    app.ctx.services.restart = spy
    app.ctx.store.patch({"home_assistant": {"enabled": True, "url": "http://127.0.0.1:1"}})

    await asyncio.wait_for(restarted.wait(), timeout=5)


async def test_unrelated_settings_do_not_restart_integrations(store) -> None:
    app = NovaApplication(store)
    calls: list[str] = []
    app.ctx.services.restart = lambda name: calls.append(name)  # type: ignore[assignment]

    app.ctx.store.patch({"appearance": {"theme": "ember"}})
    await asyncio.sleep(0.05)
    assert calls == []
