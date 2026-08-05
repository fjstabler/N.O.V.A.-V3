"""The local WebSocket bridge the desktop shell connects to.

Bound to loopback by default and gated on a shared token that is generated on
first run and handed to the shell through a runtime descriptor file. That
combination stops any other process on the machine — or a page in a stray
browser — from driving the assistant. Setting `transport.host` to a Tailscale
address extends the same bridge to other devices on that private network (see
`mobile_web/`) without weakening the token gate — Tailscale is the network
boundary in that case, the same way loopback is the boundary by default.

Bus events are mirrored to every connected client. High-frequency topics
(audio level, metrics) are coalesced to a fixed rate so a 60 FPS UI never gets
back-pressured by a 100 Hz producer.

Plain HTTP GETs on the same host:port (i.e. anything that isn't a WebSocket
upgrade) are served the mobile web client's static files, so one process and
one port covers both the desktop shell's WebSocket and a phone's browser tab.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import mimetypes
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from ..context import NovaContext
from ..integrations.local_camera import capture_camera_jpeg
from ..integrations.services import HomeService
from ..runtime import Service, Topics
from ..runtime.errors import ConfirmationRequired, NovaError
from .protocol import Kind, Message
from .router import RequestRouter

try:
    import websockets
    from websockets.asyncio.server import ServerConnection, serve
    from websockets.datastructures import Headers
    from websockets.http11 import Request, Response
except ImportError:  # pragma: no cover - hard dependency, guarded for clarity
    websockets = None  # type: ignore[assignment]
    ServerConnection = Any  # type: ignore[misc,assignment]
    serve = None  # type: ignore[assignment]
    Headers = Any  # type: ignore[misc,assignment]
    Request = Any  # type: ignore[misc,assignment]
    Response = Any  # type: ignore[misc,assignment]

#: The mobile web client's built files: services/core/nova/mobile_web/static/.
STATIC_ROOT = Path(__file__).resolve().parent.parent / "mobile_web" / "static"

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".webmanifest": "application/manifest+json",
}


def resolve_static_path(root: Path, url_path: str) -> Path | None:
    """Map a request path to a file under `root`, or None if there isn't one.

    `/` and any path without a file extension serve `index.html` — a phone
    reloading a deep link should still land on the app shell rather than a
    404. Resolving the joined path and checking it is still inside `root`
    stops `..` segments walking out of the static directory.
    """
    if not root.is_dir():
        return None
    path = urlsplit(url_path).path
    relative = path.lstrip("/") or "index.html"
    if "." not in Path(relative).name:
        relative = "index.html"
    candidate = (root / relative).resolve()
    if root.resolve() not in candidate.parents and candidate != root.resolve():
        return None
    return candidate if candidate.is_file() else None


#: Topics never forwarded to the UI — internal plumbing or secrets.
_PRIVATE_PREFIXES = ("internal.", "secret.")

#: Topics coalesced to at most one message per interval (seconds).
_THROTTLE: dict[str, float] = {
    Topics.AUDIO_LEVEL: 1 / 30,
    Topics.METRICS: 1.0,
}


class BridgeService(Service):
    """Serves the UI protocol over ws://127.0.0.1."""

    name = "transport"
    critical = True

    def __init__(self, ctx: NovaContext, router: RequestRouter) -> None:
        super().__init__(ctx)
        self.router = router
        self._clients: set[ServerConnection] = set()
        self._server: Any = None
        self._last_sent: dict[str, float] = {}
        self._pending_confirmations: dict[str, ConfirmationRequired] = {}
        self._bound_port: int = 0
        #: In-flight broadcast sends, held so they are not garbage collected.
        self._send_tasks: set[asyncio.Task[None]] = set()

    # -------------------------------------------------------------- lifecycle

    async def on_start(self) -> None:
        if serve is None:  # pragma: no cover
            raise NovaError("the 'websockets' package is required to run the bridge")

        cfg = self.ctx.settings.transport
        self._server = await serve(
            self._handle_client,
            cfg.host,
            cfg.port,
            ping_interval=20,
            ping_timeout=20,
            max_size=8 * 1024 * 1024,  # room for a base64 screenshot or a voice clip
            compression=None,  # loopback (or a tailnet); compression costs more than it saves
            process_request=self._process_request,
        )
        self._bound_port = _bound_port(self._server, cfg.port)
        self._write_runtime_descriptor()
        self.bus.subscribe("*", self._forward_event)
        self.log.info("bridge_listening", host=cfg.host, port=self._bound_port)

    async def on_stop(self) -> None:
        for client in tuple(self._clients):
            with contextlib.suppress(Exception):
                await client.close()
        self._clients.clear()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
        with contextlib.suppress(OSError):
            self._descriptor_path.unlink(missing_ok=True)

    def describe(self) -> str:
        host = self.ctx.settings.transport.host
        return f"{host}:{self._bound_port} · {len(self._clients)} client(s)"

    # ----------------------------------------------------------- static files

    async def _process_request(self, connection: ServerConnection, request: Request) -> Any:
        """Serve the mobile web client and camera snapshots over plain HTTP.

        `process_request` runs for every incoming request, upgrade or not.
        Returning `None` tells the library to continue with the normal
        WebSocket handshake; returning a `Response` answers the request
        directly and closes the connection, which is what a phone's browser
        needs for the HTML/JS/CSS it loads before it ever opens a socket, and
        what the desktop app's camera surface needs for a snapshot image.
        """
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return None
        url_path = urlsplit(request.path).path
        if url_path.startswith("/camera/"):
            return await self._camera_response(request, url_path)
        path = resolve_static_path(STATIC_ROOT, request.path)
        if path is None:
            return Response(404, "Not Found", Headers(), b"not found")
        body = path.read_bytes()
        content_type = _CONTENT_TYPES.get(path.suffix) or (
            mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        )
        headers = Headers()
        headers["Content-Type"] = content_type
        headers["Content-Length"] = str(len(body))
        headers["Cache-Control"] = "no-cache"
        return Response(200, "OK", headers, body)

    # ------------------------------------------------------------------ camera

    async def _camera_response(self, request: Request, url_path: str) -> Response:
        """Serve one JPEG snapshot from a camera resolved by `display.show_camera`.

        A snapshot on every request, not a persistent stream — the frontend
        polls this on an interval instead of the bridge holding open a
        continuous proxy, which stays well within what `process_request`'s
        single-response model can do.
        """
        if not self._request_authorised(request):
            return Response(401, "Unauthorized", Headers(), b"unauthorized")

        slug = unquote(url_path.removeprefix("/camera/"))
        source, _, identifier = slug.partition(":")
        image: bytes | None = None

        if source == "local" and identifier:
            camera = next(
                (c for c in self.ctx.settings.vision.named_cameras if c.name == identifier), None
            )
            if camera is not None:
                try:
                    image = await asyncio.to_thread(capture_camera_jpeg, camera.index)
                except Exception as exc:  # noqa: BLE001 - a camera glitch should not crash the bridge
                    self.log.warning(
                        "local_camera_snapshot_failed", name=identifier, error=str(exc)
                    )
        elif source == "ha" and identifier:
            home = self.ctx.service("home", HomeService)
            if home is not None and home.ha is not None:
                image = await home.ha.camera_snapshot_jpeg(identifier)

        if image is None:
            return Response(404, "Not Found", Headers(), b"camera unavailable")

        headers = Headers()
        headers["Content-Type"] = "image/jpeg"
        headers["Content-Length"] = str(len(image))
        headers["Cache-Control"] = "no-store"
        return Response(200, "OK", headers, image)

    def _request_authorised(self, request: Request) -> bool:
        """Token check for a plain HTTP request — the WS path's `_authorised`
        reads the same token from a live `connection`, this reads it from the
        `Request` handed to `process_request` before any connection exists."""
        expected = self.ctx.settings.transport.token
        if not expected:
            return True
        _, _, query = request.path.partition("?")
        for part in query.split("&"):
            key, _, value = part.partition("=")
            if key == "token":
                return _constant_time_equals(value, expected)
        header = request.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            return _constant_time_equals(header[7:], expected)
        return False

    # ------------------------------------------------------------- descriptor

    @property
    def _descriptor_path(self):  # type: ignore[no-untyped-def]
        return self.ctx.paths.data_dir / "bridge.json"

    def _write_runtime_descriptor(self) -> None:
        """Publish connection details for the shell to pick up.

        Written to the data dir (0600) *and* echoed on stdout, so the shell works
        whether it spawned the core as a child process or attached to one that
        was already running as a system service.
        """
        cfg = self.ctx.settings.transport
        descriptor = {
            "host": cfg.host,
            "port": self._bound_port,
            "token": cfg.token,
            "pid": os.getpid(),
            "version": 1,
            "startedAt": time.time(),
        }
        path = self._descriptor_path
        try:
            path.write_text(json.dumps(descriptor), encoding="utf-8")
            if sys.platform != "win32":
                path.chmod(0o600)
        except OSError as exc:
            self.log.warning("descriptor_write_failed", error=str(exc))
        # Consumed by the Electron main process when it spawns us.
        print(f"NOVA_BRIDGE_READY {json.dumps(descriptor)}", flush=True)

    # ---------------------------------------------------------------- clients

    async def _handle_client(self, connection: ServerConnection) -> None:
        if not self._authorised(connection):
            await connection.close(code=4401, reason="unauthorised")
            self.log.warning("bridge_unauthorised", peer=str(connection.remote_address))
            return

        self._clients.add(connection)
        self.log.info("ui_connected", clients=len(self._clients))
        try:
            await self._send(connection, self._hello_message())
            async for raw in connection:
                await self._on_message(connection, raw)
        except Exception as exc:  # noqa: BLE001 - connection teardown is expected
            if websockets and not isinstance(exc, websockets.exceptions.ConnectionClosed):
                self.log.warning("ui_connection_error", error=str(exc))
        finally:
            self._clients.discard(connection)
            self.log.info("ui_disconnected", clients=len(self._clients))

    def _authorised(self, connection: ServerConnection) -> bool:
        # Token arrives as ?token=… ; the shell reads it from the descriptor file.
        return self._request_authorised(connection.request)

    def _hello_message(self) -> Message:
        return Message(
            Kind.HELLO,
            "hello",
            {
                "service": "nova-core",
                "version": "3.0.0",
                "state": self.ctx.state.state.value,
                "routes": self.router.topics,
                "capabilities": self.ctx.services.health_report(),
            },
        )

    # --------------------------------------------------------------- requests

    async def _on_message(self, connection: ServerConnection, raw: str | bytes) -> None:
        try:
            message = Message.decode(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            await self._send(connection, Message.error("", "protocol", "nova.protocol", str(exc)))
            return

        if message.kind is not Kind.REQUEST:
            return

        handler = self.router.get(message.topic)
        if handler is None:
            await self._send(
                connection,
                Message.error(
                    message.id, message.topic, "nova.unknown_route", f"no route '{message.topic}'"
                ),
            )
            return

        if self.ctx.settings.developer.trace_tool_calls:
            self.log.debug("ui_request", topic=message.topic)

        try:
            result = await handler(message.payload)
            await self._send(connection, Message.response(message.id, message.topic, result))
        except ConfirmationRequired as exc:
            self._pending_confirmations[exc.token] = exc
            await self._send(
                connection,
                Message.error(message.id, message.topic, exc.code, exc.message, **exc.as_payload()),
            )
        except NovaError as exc:
            await self._send(
                connection, Message.error(message.id, message.topic, exc.code, exc.message)
            )
        except Exception as exc:
            self.log.exception("request_handler_failed", topic=message.topic)
            await self._send(
                connection, Message.error(message.id, message.topic, "nova.internal", str(exc))
            )

    # ----------------------------------------------------------------- events

    def _forward_event(self, event: Any) -> None:
        topic: str = event.topic
        if topic.startswith(_PRIVATE_PREFIXES):
            return
        interval = _THROTTLE.get(topic)
        if interval is not None:
            now = time.monotonic()
            if now - self._last_sent.get(topic, 0.0) < interval:
                return
            self._last_sent[topic] = now
        self.broadcast(Message.event(topic, event.payload))

    def broadcast(self, message: Message) -> None:
        if not self._clients:
            return
        encoded = message.encode()
        for client in tuple(self._clients):
            # Hold a reference until the send completes; a task that only the
            # event loop knows about can be garbage collected mid-flight.
            task = asyncio.create_task(self._send_raw(client, encoded))
            self._send_tasks.add(task)
            task.add_done_callback(self._send_tasks.discard)

    async def _send(self, connection: ServerConnection, message: Message) -> None:
        await self._send_raw(connection, message.encode())

    async def _send_raw(self, connection: ServerConnection, encoded: str) -> None:
        try:
            await connection.send(encoded)
        except Exception:  # noqa: BLE001 - client vanished mid-send
            self._clients.discard(connection)

    # ---------------------------------------------------------- confirmations

    def stash_confirmation(self, request: ConfirmationRequired) -> None:
        self._pending_confirmations[request.token] = request

    def take_confirmation(self, token: str) -> ConfirmationRequired | None:
        return self._pending_confirmations.pop(token, None)

    @property
    def client_count(self) -> int:
        return len(self._clients)


def _bound_port(server: Any, fallback: int) -> int:
    with contextlib.suppress(Exception):
        for sock in server.sockets:
            return int(sock.getsockname()[1])
    return fallback


def _constant_time_equals(a: str, b: str) -> bool:
    import hmac

    return hmac.compare_digest(a, b)
