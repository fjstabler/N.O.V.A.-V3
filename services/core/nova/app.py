"""Application assembly.

The one place that knows the whole system. Everything else depends on the
context and the bus, never on a sibling — so this file is the complete map of
what N.O.V.A. is made of, and the only file to touch when adding a subsystem.

Start order is derived from declared dependencies, not written here; see
:meth:`ServiceManager.resolve_order`.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import signal
import sys
from typing import Any

from .ai.orchestrator import Orchestrator
from .config import SettingsStore, describe_settings
from .context import NovaContext
from .frigate_watch import FrigateService
from .integrations.services import CalendarService, HomeLabService, HomeService
from .memory.service import MemoryService
from .notifications import NotificationService
from .presence import PresenceService
from .runtime import NovaState, Topics, configure_logging, get_logger
from .runtime.errors import NovaError
from .security.service import SecurityService
from .skills.registry import SkillRegistry
from .system.service import SystemService
from .transport.protocol import Requests
from .transport.router import RequestRouter
from .transport.server import BridgeService
from .voice.audio import samples_to_wav_base64
from .voice.service import VoiceService

log = get_logger(__name__)

VERSION = "4.0.0"

#: Config sections each integration service reads at start. Editing any of them
#: restarts that service, so a newly-entered URL or token takes effect without a
#: process restart. Voice and system are absent deliberately: they hold a
#: microphone and a metrics loop, and reload their own settings in place.
_SERVICE_SECTIONS: dict[str, tuple[str, ...]] = {
    "home": ("home_assistant", "mqtt"),
    "homelab": ("homelab",),
    "calendar": ("calendar",),
}


class NovaApplication:
    """Owns the process lifecycle."""

    def __init__(self, store: SettingsStore | None = None) -> None:
        self.ctx = NovaContext(store)
        self.router = RequestRouter()
        self._stopping = asyncio.Event()
        #: Restarts in flight, held so they are not garbage collected.
        self._restarts: set[asyncio.Task[None]] = set()
        self._register_services()
        self._register_routes()
        self.ctx.store.on_change(self._on_settings_changed)

    # ---------------------------------------------------------------- assembly

    def _register_services(self) -> None:
        """Declare the service graph. Start order among services with no
        `requires` on each other falls out of registration order below (see
        `ServiceManager.resolve_order`) — which matters more than the name
        "order is irrelevant" suggests. `SkillRegistry.on_start()` calls each
        skill's `is_available()` exactly once, synchronously, during its own
        start; a skill that checks `ctx.service(X)` there needs X registered,
        and therefore started, before `SkillRegistry` below, or it reads that
        service as not-yet-running and its tools never make it into the
        model's catalogue — silently, for the rest of the process, since nothing
        ever re-checks it (`reevaluate()` only fires for home/homelab/calendar
        settings changes, not at boot). Home, HomeLab and Calendar are placed
        correctly for exactly this reason; Security was not, until this comment.
        """
        register = self.ctx.services.register
        register(BridgeService(self.ctx, self.router))
        register(MemoryService(self.ctx))
        register(SystemService(self.ctx))
        register(HomeService(self.ctx))
        register(HomeLabService(self.ctx))
        register(CalendarService(self.ctx))
        register(SecurityService(self.ctx))
        register(PresenceService(self.ctx))
        register(FrigateService(self.ctx))
        register(SkillRegistry(self.ctx))
        register(Orchestrator(self.ctx))
        register(NotificationService(self.ctx))
        register(VoiceService(self.ctx))

    def _register_routes(self) -> None:
        """Every request the UI is allowed to make."""
        route = self.router.route

        @route(Requests.HELLO)
        async def hello(_: dict[str, Any]) -> dict[str, Any]:
            return {
                "version": VERSION,
                "state": self.ctx.state.state.value,
                "services": self.ctx.services.health_report(),
                "voice": self._voice_status(),
                "skills": [s.name for s in self.ctx.skills.catalogue()] if self.ctx.skills else [],
            }

        @route(Requests.SETTINGS_GET)
        async def settings_get(_: dict[str, Any]) -> dict[str, Any]:
            return {"settings": self.ctx.store.public_dict()}

        @route(Requests.SETTINGS_SCHEMA)
        async def settings_schema(_: dict[str, Any]) -> dict[str, Any]:
            return {"sections": describe_settings(), "secrets": self.ctx.store.secret_field_paths()}

        @route(Requests.SETTINGS_SET)
        async def settings_set(payload: dict[str, Any]) -> dict[str, Any]:
            patch = payload.get("patch") or payload.get("settings") or {}
            if not isinstance(patch, dict):
                raise NovaError("settings patch must be an object")
            updated = self.ctx.store.patch(patch)
            public = self.ctx.store.public_dict()
            self.ctx.bus.publish(Topics.SETTINGS_UPDATED, {"settings": public}, source="app")
            _ = updated
            return {"settings": public}

        @route(Requests.VOICE_ACTIVATE)
        async def voice_activate(_: dict[str, Any]) -> dict[str, Any]:
            voice = self.ctx.service("voice", VoiceService)
            if voice is None:
                return {"activated": False, "reason": "voice is unavailable"}
            return {"activated": await voice.activate()}

        @route(Requests.VOICE_CANCEL)
        async def voice_cancel(_: dict[str, Any]) -> dict[str, Any]:
            voice = self.ctx.service("voice", VoiceService)
            if voice is not None:
                await voice.cancel()
            orchestrator = self.ctx.service("orchestrator", Orchestrator)
            if orchestrator is not None:
                orchestrator.cancel_current()
            return {"cancelled": True}

        @route(Requests.TEXT_SUBMIT)
        async def text_submit(payload: dict[str, Any]) -> dict[str, Any]:
            text = str(payload.get("text", "")).strip()
            if not text:
                raise NovaError("no text provided")
            orchestrator = self.ctx.service("orchestrator", Orchestrator)
            if orchestrator is None:
                raise NovaError("reasoning is unavailable")
            # The mobile client's text fallback sends source="mobile" so its
            # replies stay off the home speaker, same as its voice input does;
            # the desktop console omits it and keeps today's behaviour.
            source = str(payload.get("source") or "text")
            result = await orchestrator.handle(text, source=source)
            audio = await self._mobile_audio(result.text) if source == "mobile" else None
            return {
                "text": result.text,
                "tools": result.tools_used,
                "error": result.error,
                "ms": result.duration_ms,
                "audio": audio,
            }

        @route(Requests.VOICE_AUDIO_SUBMIT)
        async def voice_audio_submit(payload: dict[str, Any]) -> dict[str, Any]:
            """Push-to-talk from a remote client: a full recording, not a stream.

            Used by the mobile web client — it has no wake word or endpointer of
            its own, it just records while a button is held and sends the whole
            utterance at once. Runs through the same transcriber and orchestrator
            a local wake word would, so it gets the same tools, memory and
            reasoning; only the reply's destination differs (see `source="mobile"`
            in the orchestrator, which skips the local speaker for it).
            """
            voice = self.ctx.service("voice", VoiceService)
            if voice is None or not voice.transcriber.loaded:
                raise NovaError("speech recognition is unavailable")

            raw = str(payload.get("audio", ""))
            if not raw:
                raise NovaError("no audio provided")
            try:
                audio = base64.b64decode(raw, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise NovaError("audio was not valid base64") from exc

            transcript = await voice.transcriber.transcribe(audio)
            if transcript.error:
                raise NovaError(transcript.error)
            if not transcript.usable:
                return {
                    "transcript": transcript.text,
                    "text": "",
                    "tools": [],
                    "error": None,
                    "ms": 0,
                }

            orchestrator = self.ctx.service("orchestrator", Orchestrator)
            if orchestrator is None:
                raise NovaError("reasoning is unavailable")
            result = await orchestrator.handle(transcript.text, source="mobile")
            audio = await self._mobile_audio(result.text)
            return {
                "transcript": transcript.text,
                "text": result.text,
                "tools": result.tools_used,
                "error": result.error,
                "ms": result.duration_ms,
                "audio": audio,
            }

        @route(Requests.CONFIRM)
        async def confirm(payload: dict[str, Any]) -> dict[str, Any]:
            token = str(payload.get("token", ""))
            approved = bool(payload.get("approved", True))
            registry = self.ctx.skills
            if registry is None:
                raise NovaError("skills are unavailable")
            if not approved:
                return {"cancelled": registry.cancel(token)}
            orchestrator = self.ctx.service("orchestrator", Orchestrator)
            if orchestrator is None:
                raise NovaError("reasoning is unavailable")
            result = await orchestrator.confirm(token)
            return {"text": result.text, "error": result.error}

        @route(Requests.METRICS_GET)
        async def metrics_get(_: dict[str, Any]) -> dict[str, Any]:
            system = self.ctx.service("system", SystemService)
            if system is None:
                return {"available": False}
            return {"available": True, **await system.snapshot()}

        @route(Requests.SERVICES_GET)
        async def services_get(_: dict[str, Any]) -> dict[str, Any]:
            return {"services": self.ctx.services.health_report()}

        @route(Requests.SKILLS_GET)
        async def skills_get(_: dict[str, Any]) -> dict[str, Any]:
            registry = self.ctx.skills
            if registry is None:
                return {"skills": []}
            return {
                "skills": [
                    {
                        "name": info.name,
                        "description": info.description,
                        "available": info.available,
                        "reason": info.reason,
                        "tools": info.tools,
                    }
                    for info in registry.catalogue()
                ]
            }

        @route(Requests.AUDIO_DEVICES)
        async def audio_devices(_: dict[str, Any]) -> dict[str, Any]:
            return {"devices": VoiceService.devices(), "voice": self._voice_status()}

        @route(Requests.NOTIFICATION_DISMISS)
        async def notification_dismiss(payload: dict[str, Any]) -> dict[str, Any]:
            service = self.ctx.service("notifications", NotificationService)
            if service is None:
                return {"dismissed": False}
            return {"dismissed": service.dismiss(str(payload.get("id", "")))}

        @route(Requests.HOME_ENTITIES)
        async def home_entities(payload: dict[str, Any]) -> dict[str, Any]:
            home = self.ctx.service("home", HomeService)
            if home is None or home.ha is None:
                return {"entities": [], "connected": False}
            domain = str(payload.get("domain", ""))
            return {
                "connected": True,
                "entities": [e.as_payload() for e in home.ha.entities(domain)],
            }

        @route(Requests.HOMELAB_STATUS)
        async def homelab_status(_: dict[str, Any]) -> dict[str, Any]:
            service = self.ctx.service("homelab", HomeLabService)
            if service is None:
                return {"services": []}
            return {"services": service.snapshot()}

        @route(Requests.CALENDAR_AGENDA)
        async def calendar_agenda(payload: dict[str, Any]) -> dict[str, Any]:
            service = self.ctx.service("calendar", CalendarService)
            if service is None:
                return {"events": []}
            days = int(payload.get("days", 1))
            events = await service.agenda(days=max(1, min(days, 30)))
            return {"events": [e.as_payload() for e in events]}

        @route(Requests.APP_QUIT)
        async def app_quit(_: dict[str, Any]) -> dict[str, Any]:
            self.request_stop()
            return {"stopping": True}

    def _voice_status(self) -> dict[str, Any]:
        voice = self.ctx.service("voice", VoiceService)
        return voice.status() if voice is not None else {"state": "unavailable"}

    async def _mobile_audio(self, text: str) -> str | None:
        """Synthesise a reply as base64 WAV for the mobile client's own player.

        The mobile page used to read replies aloud with the browser's
        speechSynthesis API. In practice that was unreliable on real iOS
        hardware — silent even after working around its user-gesture rule and
        its habit of garbage-collecting an utterance before it plays. Playing
        a single, once-unlocked <audio> element is the pattern iOS actually
        honours consistently, so replies destined for the mobile client are
        rendered here with the same Kokoro voice the desktop hears, and
        shipped as bytes instead.
        """
        if not text.strip():
            return None
        voice = self.ctx.service("voice", VoiceService)
        if voice is None or not voice.synthesiser.loaded:
            return None
        result = await voice.synthesiser.synthesise(text)
        if result is None:
            return None
        samples, sample_rate = result
        return samples_to_wav_base64(samples, sample_rate)

    # --------------------------------------------------------------- lifecycle

    def _on_settings_changed(self, settings: Any, changed: dict[str, Any]) -> None:
        """Restart any integration whose configuration just changed."""
        if self.ctx.shutting_down:
            return
        touched = {path.split(".", 1)[0] for path in changed}
        for name, sections in _SERVICE_SECTIONS.items():
            if touched.isdisjoint(sections):
                continue
            task = asyncio.create_task(self._restart_integration(name))
            self._restarts.add(task)
            task.add_done_callback(self._restarts.discard)

    async def _restart_integration(self, name: str) -> None:
        from .runtime.service import ServiceState

        try:
            state = await self.ctx.services.restart(name)
        except Exception:  # a failed restart must not take the app down
            log.exception("integration_restart_failed", service=name)
            return

        service = self.ctx.services.get(name)
        detail = (service.health.detail or "") if service is not None else ""

        # A service coming up does not by itself put its skill's tools in front
        # of the model — is_available() was only ever checked once, at boot.
        # Without this, "connect Home Assistant" and "she can control lights"
        # are two different events, and the gap between them looks exactly
        # like the integration not working at all.
        newly_available: set[str] = set()
        if state is ServiceState.RUNNING and self.ctx.skills is not None:
            newly_available = await self.ctx.skills.reevaluate()

        if state is ServiceState.RUNNING:
            body = detail
            if newly_available:
                skills = ", ".join(sorted(newly_available))
                plural = "s" if len(newly_available) != 1 else ""
                body = f"{detail} — {skills} skill{plural} now available"
            self._notify("success", f"{name.title()} connected", body)
        else:
            self._notify("warning", f"{name.title()} unavailable", detail or state.value)

    def _notify(self, level: str, title: str, body: str) -> None:
        self.ctx.bus.publish(
            Topics.NOTIFICATION,
            {"level": level, "title": title, "body": body, "source": "app", "timeout": 9.0},
            source="app",
        )

    async def start(self) -> None:
        log.info("nova_starting", version=VERSION, config=str(self.ctx.paths.config_file))
        await self.ctx.services.start_all()
        await self.ctx.state.transition(NovaState.IDLE, reason="boot-complete")
        self.ctx.bus.publish(
            Topics.READY,
            {"version": VERSION, "services": self.ctx.services.health_report()},
            source="app",
        )
        degraded = [
            s["name"] for s in self.ctx.services.health_report() if s["state"] == "degraded"
        ]
        log.info("nova_ready", degraded=degraded or None)

    async def stop(self) -> None:
        if self.ctx.shutting_down:
            return
        self.ctx.shutting_down = True
        log.info("nova_stopping")
        await self.ctx.bus.publish_and_wait(Topics.SHUTDOWN, {}, source="app")
        await self.ctx.services.stop_all()
        await self.ctx.state.close()
        await self.ctx.bus.close()
        log.info("nova_stopped")

    def request_stop(self) -> None:
        self._stopping.set()

    async def run_forever(self) -> None:
        """Start, install signal handlers, and block until asked to stop."""
        loop = asyncio.get_running_loop()
        for signal_name in ("SIGINT", "SIGTERM"):
            signal_number = getattr(signal, signal_name, None)
            if signal_number is None:
                continue
            with contextlib.suppress(NotImplementedError):
                # Windows' proactor loop has no add_signal_handler.
                loop.add_signal_handler(signal_number, self.request_stop)

        await self.start()
        try:
            await self._stopping.wait()
        finally:
            await self.stop()


async def main_async(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="nova-core", description="N.O.V.A. core service")
    parser.add_argument("--config", help="path to a config.toml (overrides the default location)")
    parser.add_argument("--log-level", default="", help="DEBUG, INFO, WARNING or ERROR")
    parser.add_argument("--json-logs", action="store_true", help="emit structured JSON logs")
    parser.add_argument("--port", type=int, default=0, help="override the bridge port")
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    args = parser.parse_args(argv)

    if args.version:
        print(f"nova-core {VERSION}")
        return 0

    store = _build_store(args.config)
    developer = store.settings.developer
    configure_logging(
        args.log_level or developer.log_level,
        json_output=args.json_logs or developer.json_logs,
        log_file=store.paths.log_file,
    )
    if args.port:
        store.patch({"transport": {"port": args.port}}, persist=False)

    app = NovaApplication(store)
    try:
        await app.run_forever()
    except NovaError as exc:
        log.error("startup_failed", error=exc.message, detail=exc.detail)
        return 1
    return 0


def _build_store(config_path: str | None) -> SettingsStore:
    if not config_path:
        return SettingsStore()
    from pathlib import Path

    from .config.paths import NovaPaths

    path = Path(config_path).expanduser().resolve()
    root = path.parent
    return SettingsStore(NovaPaths(root, root, root / "cache"))


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(main_async(argv))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
