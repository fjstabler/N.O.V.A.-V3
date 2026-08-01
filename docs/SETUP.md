# Setup

## Requirements

- Python 3.11 or newer
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
pip install -e "services/core[voice]"
make models      # downloads Kokoro; prepares openWakeWord
```

The core runs happily without it — every stage degrades independently and tells
you what is missing.

#### Wake word

`hey nova` is **not** one of openWakeWord's bundled phrases. Two options:

1. Train one at [openWakeWord](https://github.com/dscripka/openWakeWord) — it
   takes a few minutes and needs no recordings — then point
   `voice.wake.model` at the resulting `.onnx` file.
2. Use a bundled phrase in the meantime: set `voice.wake.model` to `hey_jarvis`
   or `alexa` and `voice.wake.phrase` to match.

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
correct the instant someone flips a physical switch.

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
