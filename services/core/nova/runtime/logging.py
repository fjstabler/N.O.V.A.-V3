"""Structured logging.

``structlog`` when available, otherwise a stdlib shim exposing the same
key-value call style so no module has to care which one is active.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Protocol

try:  # pragma: no cover - exercised by whichever branch the host has
    import structlog

    _HAS_STRUCTLOG = True
except ImportError:  # pragma: no cover
    structlog = None  # type: ignore[assignment]
    _HAS_STRUCTLOG = False


class Logger(Protocol):
    def debug(self, event: str, **kw: Any) -> None: ...
    def info(self, event: str, **kw: Any) -> None: ...
    def warning(self, event: str, **kw: Any) -> None: ...
    def error(self, event: str, **kw: Any) -> None: ...
    def exception(self, event: str, **kw: Any) -> None: ...


class _StdlibLogger:
    """Minimal key-value adapter used when structlog is not installed."""

    def __init__(self, name: str) -> None:
        self._log = logging.getLogger(name)

    @staticmethod
    def _fmt(event: str, kw: dict[str, Any]) -> str:
        if not kw:
            return event
        pairs = " ".join(f"{k}={v!r}" for k, v in kw.items())
        return f"{event} {pairs}"

    def debug(self, event: str, **kw: Any) -> None:
        self._log.debug(self._fmt(event, kw))

    def info(self, event: str, **kw: Any) -> None:
        self._log.info(self._fmt(event, kw))

    def warning(self, event: str, **kw: Any) -> None:
        self._log.warning(self._fmt(event, kw))

    def error(self, event: str, **kw: Any) -> None:
        self._log.error(self._fmt(event, kw))

    def exception(self, event: str, **kw: Any) -> None:
        self._log.exception(self._fmt(event, kw))


_configured = False


def configure(
    level: str = "INFO", *, json_output: bool = False, log_file: Path | None = None
) -> None:
    """Install log handlers. Safe to call more than once."""
    global _configured

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        format="%(message)s"
        if _HAS_STRUCTLOG
        else "%(asctime)s %(levelname)-7s %(name)s │ %(message)s",
        level=getattr(logging, level.upper(), logging.INFO),
        handlers=handlers,
        force=True,
    )
    # These libraries are chatty at INFO and drown out our own events.
    for noisy in ("httpx", "httpcore", "websockets", "urllib3", "openai", "faster_whisper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if _HAS_STRUCTLOG:
        renderer = (
            structlog.processors.JSONRenderer()
            if json_output
            else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
        )
        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="%H:%M:%S", utc=False),
                structlog.processors.StackInfoRenderer(),
                structlog.processors.format_exc_info,
                renderer,
            ],
            wrapper_class=structlog.make_filtering_bound_logger(
                getattr(logging, level.upper(), logging.INFO)
            ),
            logger_factory=structlog.stdlib.LoggerFactory(),
            cache_logger_on_first_use=True,
        )
    _configured = True


def get_logger(name: str) -> Logger:
    if _HAS_STRUCTLOG:
        return structlog.get_logger(name)  # type: ignore[no-any-return]
    return _StdlibLogger(name)
