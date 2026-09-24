"""N.O.V.A. — Neural Operational Virtual Assistant.

A local-first assistant core. Voice, memory, system control and home automation
run on the user's own hardware; only reasoning reaches the network.

The public surface is deliberately small: build a :class:`~nova.app.NovaApplication`
and run it. Everything else is reached through :class:`~nova.context.NovaContext`.
"""

from __future__ import annotations

__version__ = "3.0.0"
__all__ = ["NovaApplication", "NovaContext", "__version__", "main"]


def __getattr__(name: str) -> object:
    # Lazy re-exports: importing `nova` should not pull in the whole service graph.
    if name in ("NovaApplication", "main"):
        from . import app

        return getattr(app, name)
    if name == "NovaContext":
        from .context import NovaContext

        return NovaContext
    raise AttributeError(f"module 'nova' has no attribute '{name}'")
