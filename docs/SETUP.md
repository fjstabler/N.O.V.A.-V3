# Setup

## Requirements

- Python 3.11–3.13 (3.14 works, minus the wake word — see below)
- Node.js 20 or newer
- Linux, Windows or macOS
- An OpenAI API key (the only paid dependency)

On Debian/Ubuntu the voice stack also needs PortAudio:

```bash
sudo apt install libportaudio2 ffmpeg
```

An NVIDIA GPU is optional. With one, transcription runs in float16 on CUDA;
without one it falls back to int8 on CPU automatically.

## Install

```bash
make setup      # venv + core (with AI, memory, home, server extras) + desktop
```

Or by hand:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e "services/core[dev,ai,embeddings,home,server]"
cd apps/desktop && npm install
```

### The voice stack

Kept separate because it pulls several hundred megabytes of ML wheels:

```bash
pip install -e "services/core[voice]"   # microphone, Whisper, Kokoro
pip install -e "services/core[wake]"    # wake word — see the caveat below
make models
```

`wake` is a separate extra on purpose. openWakeWord declares `tflite-runtime`
on Linux, which has no wheel for the newest Python releases — on Python 3.14
the install fails. Because pip installs an extra atomically, bundling it with
the rest would take the microphone down with it. Split out, everything else
installs and push-to-talk works regardless.

**Python 3.11–3.13 is the sweet spot.** The base service runs on 3.14, but
parts of the ML ecosystem do not yet. If `[wake]` will not install, that is
why.

The core runs happily without it — every stage degrades independently and tells
you what is missing.

#### Wake word

```bash
make wake
```

This installs openWakeWord *without* its dependency list, then adds what it
actually imports. openWakeWord declares `tflite-runtime` on Linux but never
touches it when driven through ONNX, which is how N.O.V.A. drives it — and that
declaration alone blocks installation on any Python without a tflite wheel.
Verified: the engine constructs and scores frames with ONNX and no tflite
present at all.

**`hey nova` is not a bundled phrase.** openWakeWord ships exactly six:
`alexa`, `hey_jarvis`, `hey_mycroft`, `hey_rhasspy`, `timer`, `weather`.

To use one now, set **Voice → Wake → Model** to `hey_jarvis` and **Phrase** to
match.

To get "Hey Nova" properly, train a model with the
[openWakeWord training notebook](https://github.com/dscripka/openWakeWord#training-new-models).
It generates its own synthetic speech, so you record nothing; expect an hour
of unattended compute on Colab's free tier. Drop the resulting `.onnx` into
`~/.local/share/nova/models/` and set **Model** to its filename.

## Run

```bash
make dev
```

The shell starts the core itself. If a core is already listening — as a systemd
unit, say — the shell attaches to it instead of spawning a second one.

To run them separately:

```bash
make dev-core      # terminal 1
make dev-desktop   # terminal 2
```

## Configure

Everything is in the settings panel (`Ctrl+,`). It writes
`~/.config/nova/config.toml` (`%APPDATA%\NOVA` on Windows), which you may also
edit by hand — an invalid section falls back to defaults rather than preventing
startup.

The file is `chmod 0600` because it holds your API key and every integration
token.

### Minimum viable configuration

Paste an OpenAI key into **Reasoning → API key**. That is enough to talk to it.

### Home Assistant

Create a long-lived access token in your HA profile, then fill in
**Home Assistant → URL and token**. N.O.V.A. opens a WebSocket subscription, so
"is the kitchen light on" is answered from a live cache rather than by polling —
correct the instant someone flips a physical switch. Cameras are included by
default — see **Showing things on screen** below for what that unlocks.

### MQTT

**MQTT → host, port, credentials.** Subscribed topics are retained per topic, so
"what's the greenhouse humidity" reads the last message rather than waiting for
the next one.

### Ubuntu server

`server.file_roots` confines filesystem access; leave it empty to default to
your home directory. `server.managed_units` lists systemd units the assistant may
control.

Controlling systemd as a non-root user needs a polkit rule:

```
# /etc/polkit-1/rules.d/50-nova.rules
polkit.addRule(function(action, subject) {
  if (action.id == "org.freedesktop.systemd1.manage-units" &&
      subject.user == "youruser") {
    return polkit.Result.YES;
  }
});
```

Docker access needs your user in the `docker` group:

```bash
sudo usermod -aG docker $USER   # log out and back in
```

`server.allow_shell` enables shell pipelines and redirection. It is off by
default, and even when on, every command still goes through the classifier.

### Home lab

Add each service under **Home Lab → Services** with its kind, URL and API key.
Supported kinds: `adguard`, `uptime-kuma`, `jellyfin`, `plex`, `immich`,
`portainer`, `node-red`, `homepage`, and `generic` for a plain up/down check.

### Calendar

Add a CalDAV account under **Calendar → Accounts**. Events are stored locally
first and pushed on sync, so scheduling by voice never waits on the network.

### Showing things on screen: maps and cameras

"Show me a map of London" and "show me the front door" put something on the
desktop app's display rather than describing it in words. Maps need nothing
configured — they geocode through OpenStreetMap for free. A camera resolves
against two sources under one name:

**A Home Assistant camera** (a Ring doorbell, or anything else HA exposes as
a `camera.*` entity) — nothing extra to set up beyond Home Assistant itself
being connected; `camera` is one of the domains N.O.V.A. already syncs. Say
"show me the front door" and it resolves by the entity's friendly name, the
same fuzzy matching every other device gets.

**A camera physically attached to the machine running the core** (a USB
webcam, a laptop's built-in one) — add it under **Vision → Named cameras**
with a name and a device index. On Linux, find the index with:

```bash
v4l2-ctl --list-devices
```

which lists each camera and the `/dev/videoN` path it owns — the `N` is the
index. (`sudo apt install v4l-utils` if the command is not found.) A camera
with only one device usually shows up as index `0`; a second one attached
later is not guaranteed to land on `1` — the listing is the source of truth,
not an assumption. Needs the same `opencv-python-headless` and `pillow`
N.O.V.A. already uses for `look_at_camera` (part of the `vision` extra —
see **Vision** below).

If a name matches both a named local camera and an HA entity, the local one
wins — it is what you explicitly configured, not a guess.

### Vision

**Vision → Enable** turns on `look_at_screen` and `look_at_camera` — the
assistant describing what it sees, in words, as opposed to the display
skill's maps and cameras above, which put the thing itself on screen. Needs
its own extra:

```bash
pip install -e "services/core[vision]"
```

### Mobile web client (e.g. an iPhone, over Tailscale)

The bridge that the desktop shell talks to also serves a small, no-build-step
web client — push-to-talk plus a text fallback — from the same host and port.
It has no wake word (a backgrounded phone tab cannot listen continuously the
way the desktop app can), so it is push-to-talk by design, not a placeholder
for one.

1. **Expose the bridge on your tailnet.** Set **Connection → Host** to your
   machine's Tailscale address (`tailscale ip -4`, or its MagicDNS name) rather
   than `127.0.0.1`. The token gate is unchanged — Tailscale's private network
   is the security boundary here, the same role loopback plays by default.
2. **Get real HTTPS on that address**, which iOS requires before it will grant
   microphone access to a web page:
   ```bash
   sudo tailscale cert your-machine.your-tailnet.ts.net
   ```
   Point something at the resulting cert/key to terminate TLS in front of the
   bridge — a tiny reverse proxy (Caddy's `tailscale cert` integration, or
   `caddy reverse-proxy --from :443 --to :8765`, is the least fuss) is enough;
   N.O.V.A. itself still only speaks plain WebSocket/HTTP.
3. **Open `https://<that address>/?token=<the token from Connection → Token>`**
   on the phone once. It saves the token to the browser's local storage and
   strips it from the address bar; you will not need to paste it again unless
   you clear site data.
4. **Add it to your Home Screen** from Safari's share sheet. It opens full
   -screen from there, no browser chrome.

Replies from the mobile client are synthesised on the core with the same
Kokoro voice the desktop hears, then sent to the phone and played there — not
spoken through the desktop's speaker, so the box at home does not also
announce a question asked from somewhere else. Typed and spoken input both go
through the same reasoning and tools as the desktop app; only where the reply
is *heard* differs. (An earlier version of this page used the phone's own
browser voice instead; that was dropped after proving unreliable — silent
more often than not — on real iOS hardware.)

## Environment overrides

Any setting can be overridden with an environment variable, which is useful for
secrets you would rather not have on disk:

```bash
export NOVA_OPENAI__API_KEY=sk-...
export NOVA_TRANSPORT__PORT=8899
export NOVA_HOME=/opt/nova          # relocate config, data and models
```

Nesting uses a double underscore.

## Packaging

```bash
make package        # AppImage + deb on Linux, NSIS on Windows
```

The Python core is deliberately **not** bundled: it needs the machine's own
interpreter and GPU libraries. Install it once as above, and the packaged shell
will find and start it.

## Running the core as a service

```ini
# /etc/systemd/user/nova-core.service
[Unit]
Description=N.O.V.A. core

[Service]
ExecStart=/opt/nova/.venv/bin/python -m nova
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now nova-core
```

The shell will attach to it on launch.

## Troubleshooting

**The Core is black.** WebGL2 is unavailable. The app falls back to Canvas2D
automatically; check the console for the reason. Turning off
**Appearance → GPU acceleration** forces the fallback.

**"Core service offline" never clears.** Check the core is running and look at
`~/.local/share/nova/logs/nova.log`. The shell retries with backoff
indefinitely, so it will connect as soon as the core is up.

**It triggers on the television.** Raise `voice.wake.sensitivity`. It requires
two consecutive frames above threshold, so genuine speech still gets through.

**It hears itself.** `voice.wake.mute_while_speaking` is on by default and mutes
input during playback. If you have echo cancellation, turn it off for barge-in.

**Nothing is transcribed.** Check the microphone in **Voice → Audio → Input
device** — the field matches on a substring of the device name.
