# N.O.V.A.

**Neural Operational Virtual Assistant** — a local-first, always-available AI
interface for a desktop machine.

Voice, memory, system control and home automation all run on your own hardware.
The only thing that reaches the internet is reasoning: everyday commands go to
OpenAI, while coding, system control and involved requests are routed to
Anthropic's Claude. An OpenAI key is the only strictly required paid dependency
— the Anthropic key is optional, and everything falls back to OpenAI without it.
Everything else is open source.

---

## What it is

A fullscreen desktop application that shows the time and an animated Core, and
otherwise stays out of the way. You talk to it. It answers, and it acts — on
your Ubuntu server, your smart home, your home lab, your calendar and your
files.

There is no chat window, no sidebar, no message history. Those are interfaces
for reading; this one is for speaking.

## Architecture at a glance

```
┌──────────────────┬──────────────┬──────────┐
│ apps/desktop     │ apps/panel   │ a phone  │
│ Electron + React │ Android      │ browser  │
│                  │ kiosk        │          │
│   the same interface, built once, at /app/ │
└───────────────────┬────────────────────────┘
                    │ WebSocket, token-gated
                    │ loopback, LAN or tailnet
┌───────────────────┴────────────────────────┐
│  services/core     Python                  │
│                                            │
│   voice     wake word → STT → TTS          │
│   ai        OpenAI + Claude, tool loop     │
│   memory    SQLite + FTS5 + embeddings     │
│   skills    plugin registry                │
│   system    metrics, Docker, systemd, files│
│   home      Home Assistant, MQTT           │
│   homelab   AdGuard, Jellyfin, Portainer…  │
│   calendar  CalDAV + local store           │
└────────────────────────────────────────────┘
```

Two processes, because inference must never block a 60 FPS renderer. They speak
a small versioned protocol; see [docs/PROTOCOL.md](docs/PROTOCOL.md).

One interface, built once. The React app the Electron shell runs is also built
for a browser and served by the core at `/app/`, so a wall panel and a phone
get the same Core animation, surfaces and settings rather than a cut-down
version that drifts. A client can also lend the core its microphone and
speaker — wake word, transcription and synthesis all stay on the core, which is
what lets a headless box in a rack still be something you talk to. See
[apps/panel/README.md](apps/panel/README.md).

Subsystems inside the core communicate **only** through an event bus. Nothing
imports a sibling, which is what keeps the module graph acyclic and makes each
piece independently testable.

## Getting started

```bash
git clone <this repo> && cd N.O.V.A.-V3
make setup          # venv + core + desktop dependencies
make models         # download Kokoro and the wake word models
make dev            # launches the shell, which starts the core itself
```

Then open the settings panel (the gear, bottom-right, or `Ctrl+,`) and paste an
OpenAI API key. Full instructions, including the voice stack and the Home
Assistant and home lab wiring, are in [docs/SETUP.md](docs/SETUP.md).

### Running without the voice stack

The voice extras pull several hundred megabytes of ML wheels. The core boots
fine without them and reports exactly what is missing and how to get it:

```
voice_stage_unavailable  stage='wake word'  reason="python package 'openwakeword' is not installed"
                         remedy="pip install 'nova-core[voice]'"
```

Use `Ctrl+Shift+K` to open the text console and drive the assistant by typing
while you set the rest up.

## Everyday use

| Action | How |
| --- | --- |
| Wake it | Say “Hey Nova” |
| Push-to-talk | `Ctrl+Shift+Space` |
| Interrupt | `Escape` |
| Settings | `Ctrl+,` or the gear |
| Text console | `Ctrl+Shift+K` |
| Quit | `Ctrl+Shift+Q` |

After it answers, it keeps listening briefly, so follow-ups need no wake word.

## Safety model

The assistant runs commands on real machines, so this is not an afterthought.

- **Commands are classified before they run** — read-only, mutating, destructive
  or forbidden. Unrecognised binaries are refused by default rather than
  allowed; `sudo` raises the risk floor; a pipeline takes the risk of its worst
  segment.
- **Destructive tools do not execute on first call.** They return a single-use,
  expiring confirmation token, and N.O.V.A. asks you out loud before acting.
- **Filesystem access is confined** to roots you configure, checked *after*
  symlink resolution. Credential paths are never readable.
- **Shell pipelines are opt-in** (`server.allow_shell`), off by default.
- **Secrets never leave the process.** Config is `0600`, and the settings panel
  sees a redaction sentinel that survives a round-trip without wiping keys.

## Layout

| Path | What lives there |
| --- | --- |
| `services/core/nova/` | The assistant core |
| `apps/desktop/` | Electron shell and the WebGL2 Core renderer; also builds the browser bundle |
| `apps/panel/` | Android kiosk app — the interface plus a native microphone |
| `packages/protocol/` | Shared wire types, checked against Python in CI |
| `docs/` | Architecture, setup, protocol, plugin guide, ADRs |
| `scripts/` | Model download, protocol parity check |

## Extending it

A capability is a *skill*: a class with async methods marked `@tool`. The JSON
schema the model sees is derived from the type hints, so the signature and the
schema cannot drift.

```python
class WeatherSkill(Skill):
    name = "weather"
    description = "Look up the forecast."

    @tool("Get the current weather for a place.")
    async def current(
        self, location: Annotated[str, Param("City name")]
    ) -> str:
        ...
```

Drop the file in `nova/skills/builtin/`, ship it as a package with a
`nova.skills` entry point, or put it in your plugin directory. Nothing else in
the codebase changes. See [docs/PLUGINS.md](docs/PLUGINS.md).

## Development

```bash
make check      # protocol parity + lint + tests, i.e. what CI runs
make test       # 163 core tests, 38 desktop tests
make typecheck  # mypy + tsc
make package    # build an installable application
```

## Licence

MIT. The one paid dependency is the OpenAI API; everything else — faster-whisper,
openWakeWord, Kokoro, Electron, SQLite — is open source.
