# Architecture

This document explains *why* the system is shaped the way it is. For how to run
it, see [SETUP.md](SETUP.md); for the wire format, [PROTOCOL.md](PROTOCOL.md).

## The two-process split

The renderer runs a WebGL2 animation at 60 FPS. Whisper transcription takes
hundreds of milliseconds and pins a core; Kokoro synthesis allocates
aggressively; a Docker call can block on a socket for seconds. Running those in
the same process as the renderer would produce visible stutter on every single
utterance.

So: a Python **core service** does all the work, and an Electron **shell** does
all the drawing. They communicate over a loopback WebSocket. The shell can be
closed and reopened without disturbing the assistant, and the core can run as a
systemd unit on a headless machine with no shell at all.

## Inside the core

### The event bus is the only coupling

Subsystems never import one another. The voice pipeline does not know the
orchestrator exists; it publishes `voice.transcript.final` and something else
decides what that means. The skills do not know about the transport; they return
values and the transport serialises them.

This is the single rule that keeps the dependency graph acyclic, and it is what
makes it possible to test the state machine without an audio device or the
memory store without a network.

```
voice ──publish──▶ [ bus ] ──▶ orchestrator ──▶ skills
                      │                            │
                      ├──▶ transport ──▶ shell     │
                      └──▶ notifications ◀─────────┘
```

Delivery is fire-and-forget with a per-handler error boundary: a slow or failing
subscriber can never stall a publisher, and a subscriber that throws at 3am does
not take down the assistant.

### Services declare dependencies; the supervisor orders them

Each subsystem is a `Service` with a `requires` tuple. The manager topologically
sorts them, starts them in order and tears them down in reverse. Start order is
never written by hand, so adding a dependency cannot silently produce a
half-initialised subsystem.

### Degradation is a first-class state, not an error

A service that raises `DegradedCapability` during start is marked `degraded` and
skipped — and so is anything that depended on it. The assistant boots on a
machine with no microphone, no GPU, no Docker and no OpenAI key, and reports
precisely what is missing:

```
DegradedCapability(capability="wake word",
                   reason="python package 'openwakeword' is not installed",
                   remedy="pip install 'nova-core[voice]'")
```

This matters more than it looks. The alternative — a hard dependency list — means
a user with no smart home cannot run the assistant at all.

### The state machine drives the animation

`NovaState` is the single source of truth for what the assistant is doing, and
the Core's appearance is a pure function of it. Transitions are validated
against an explicit table, because an illegal transition would show up on screen
as a visual glitch rather than as an exception someone notices.

`ERROR` and `NOTIFYING` are *transient*: they revert automatically to whatever
they interrupted, which is what makes a notification feel like an overlay rather
than a mode change.

## The turn

```
"Hey Nova, restart Jellyfin"

wake word fires          → LISTENING
endpointer closes        → THINKING
Whisper transcribes      → publish transcript
orchestrator:
  recall memory (hybrid lexical + semantic)
  build prompt from live state
  call OpenAI with the tool catalogue
  model asks for server_restart_container
  ↳ tool is destructive → ConfirmationRequired, nothing runs
  "Restart the jellyfin container. Should I go ahead?"   → SPEAKING
"yes"                    → resolves the pending token, runs it
                         → IDLE
```

Two details shape how this *feels*:

**Sentence streaming.** The reply is cut at sentence boundaries as it arrives and
handed straight to synthesis, so speech begins about a second after you stop
talking rather than after the full reply is generated.

**Confirmation as conversation.** A destructive call does not surface as an
error. It becomes a spoken question, and your next "yes" resolves it. The
pending action is held by the registry with a short TTL, so a "yes" ten minutes
later does not reboot a server.

## Memory

One SQLite file. No vector service, no daemon.

Retrieval is hybrid: FTS5 supplies lexical matching (always available, very
fast) and — when a local embedding model is installed — cosine similarity
supplies paraphrase matching. The two rankings are blended with reciprocal rank
fusion, which needs no score calibration between them.

Semantic search is a linear scan. That is the right call here: personal memory
tops out in the low tens of thousands of rows, where a scan costs single-digit
milliseconds and an ANN index would add a dependency plus a rebuild step for
nothing.

All database work is marshalled onto a single worker thread, which satisfies
SQLite's threading contract without a lock and keeps the event loop free.

## Skills

A skill is a class; a tool is an async method on it. The JSON schema the model
sees is **derived from the method's type hints**, so the signature and the schema
cannot drift apart — the failure mode that otherwise produces a model calling a
tool with arguments it does not accept.

Discovery has three sources: built-ins, installed packages advertising a
`nova.skills` entry point, and `.py` files in the user's plugin directory.
Adding a capability therefore never means editing the registry.

Unavailable skills are hidden from the model entirely rather than failing when
called. This keeps the prompt small and stops the assistant promising things it
cannot do.

## The Core renderer

Drawn analytically in a single fragment shader rather than from geometry. Rings
are signed-distance annuli evaluated per pixel, which gives exact antialiasing
at any resolution and lets glow fall off continuously — the difference between
something that looks rendered and something that looks drawn.

Five passes: scene → particles (additive) → bright pass → separable blur
(ping-pong) → composite with ACES tone mapping, vignette and grain. The grain
matters more than it sounds: it hides the banding a smooth radial gradient shows
on an 8-bit panel.

Particles are `GL_POINTS` whose orbits are computed in the vertex shader from a
static per-particle seed and the clock, so the CPU never touches a particle
after upload.

### Motion

Every visual parameter is a critically damped spring. Springs are used rather
than easing curves because they are *interruptible*: a state change mid-transition
retargets from wherever the value currently is, at whatever velocity it has,
with no discontinuity. Easings have to restart.

Frame time is watched with a rolling median. Sustained overruns step the quality
tier down — fewer pixels rather than dropped frames. Degradation is one-way
within a session, because oscillating between tiers is more visible than simply
running at the lower one.

## Configuration

One pydantic tree describes everything the settings panel can change, and the
panel's controls are **generated from that schema's field metadata**. Adding a
field makes it appear in the UI with the right control, bounds and help text.
There is no second list to maintain and therefore no way for the two to drift.

Secrets are marked in the schema. The store redacts them before anything leaves
the process and treats the redaction sentinel as "unchanged" on the way back, so
the panel can round-trip the whole document without ever seeing — or
accidentally erasing — a credential.

## Where the risk is

The command classifier in `nova/system/shell.py` is the highest-consequence code
in the project: if it mislabels a destructive command as read-only, a language
model can wipe a server without asking. It is allowlist-based (unrecognised
means refused), and it has the densest test coverage in the repository.
