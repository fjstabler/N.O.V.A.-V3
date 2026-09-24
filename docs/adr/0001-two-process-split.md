# 1. Split the assistant into a core service and a desktop shell

**Status:** accepted

## Context

The spec asks for a 60 FPS animated interface *and* local speech recognition,
synthesis, and system administration on the same machine.

Whisper transcription takes hundreds of milliseconds and pins a core. Kokoro
synthesis allocates aggressively. A Docker or systemd call can block on a socket
for seconds. Any of these sharing a process with the renderer produces visible
stutter on every utterance — precisely when the user is watching the Core react.

## Decision

Two processes: a Python **core service** that does all the work, and an Electron
**shell** that does all the drawing. They communicate over a token-gated
loopback WebSocket.

## Consequences

**Good.** The renderer is never blocked by inference. The shell can be closed and
reopened without disturbing the assistant. The core can run headless as a
systemd unit — useful on the Ubuntu server the spec targets, which may have no
display at all. Each side is testable without the other.

**Bad.** A protocol to define, version and keep in sync (mitigated by
`scripts/check_protocol.py`, which fails CI on drift). A second runtime to
install. Startup involves a handshake, so the shell must handle "core not up
yet" — which it must anyway, since the core may be a separate service.

## Alternatives considered

**Single Python process with a native UI toolkit.** No IPC, but no toolkit gives
the shader-driven visual the spec describes, and inference would still block the
UI thread.

**Single Electron process with Node-native ML.** The Python ecosystem is where
faster-whisper, openWakeWord and Kokoro actually live. Node ports are immature
and would mean tracking upstream changes in three libraries indefinitely.

**Web workers for inference.** Would need the models compiled to WASM, giving up
CUDA — a large performance loss on exactly the hardware the user has.
