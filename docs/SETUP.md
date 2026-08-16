# Setup

## Requirements

- Python 3.11–3.13 (3.14 works, minus the wake word — see below)
- Node.js 20 or newer
- Linux, Windows or macOS
- An OpenAI API key (the everyday model — the only strictly required paid key)
- Optionally, an Anthropic API key for the advanced reasoning tier (see below);
  without one, everything still runs on OpenAI

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

### Advanced reasoning (Claude)

N.O.V.A. runs two reasoning tiers. Everyday commands — "turn off the kitchen
light", "what's the weather", "set a timer" — stay on the fast OpenAI model.
Coding, controlling parts of your system, and genuinely involved requests are
routed to Anthropic's Claude (Sonnet 5 by default), which is stronger at exactly
those things.

Paste an Anthropic key into **Advanced reasoning → API key** and the routing
turns on automatically. Nothing else is required — the everyday tier is
untouched, and each turn is sent to whichever model suits it.

- **No Anthropic key?** Everything keeps working. Every request — coding
  included — falls back to OpenAI, so N.O.V.A. is never worse than V3 for the
  want of a second key.
- **Only an Anthropic key?** The everyday tier borrows Claude too rather than
  refusing, so a single key is a complete configuration either way.
- **Auto-route** decides per turn using the wording of the request. Turn it off
  (**Advanced reasoning → Auto-route**) to keep everything on the fast tier
  *except* when you ask explicitly — "think hard about…", "use Claude for…".
- **Model and budget.** `model` and `max_output_tokens` are adjustable; the
  default 4096-token budget covers both Claude's reply and, on Sonnet 5, its own
  reasoning.

Install the SDK with the `ai` extra (already included in `make setup`):

```bash
pip install -e "services/core[ai]"   # openai + anthropic
```

If the `anthropic` package is missing, the advanced tier simply reports itself
unavailable and the everyday tier carries on.

### Home Assistant

Create a long-lived access token in your HA profile, then fill in
**Home Assistant → URL and token**. N.O.V.A. opens a WebSocket subscription, so
"is the kitchen light on" is answered from a live cache rather than by polling —
correct the instant someone flips a physical switch. Cameras are included by
default — see **Showing things on screen** below for what that unlocks.

Beyond controlling one device at a time, N.O.V.A. understands the home as a
whole:

- **Whole-room control** — "turn off the bedroom", "turn off all the lights in
  the kitchen". It resolves the room (fuzzily: "bedroom" finds "Main Bedroom"),
  gathers the lights, switches, fans and media players in it, and switches them
  in one call. Covers and locks are never swept up by a blanket room command —
  those keep their own explicit "open the blinds" / "lock the door".
- **House overview** — "how's the house", "did I leave anything on" lists what's
  on, thermostat readings, and anything left open or unlocked.
- **Climate readout** — "how warm is it", "what's the temperature upstairs"
  reads every thermostat and temperature sensor.

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

### Desktop control

Where the server tools administer a machine, the **Desktop** skill acts on the
one N.O.V.A. is sitting at — the computer with the screen in front of you. It can
open web pages ("pull up the news"), open files and folders in their default app
("open that invoice"), launch applications by name ("open the terminal"), and
read or set the clipboard.

These actions are reversible — a page or app can be closed again in a second — so
they run **without** the confirmation gate. The genuinely risky actions (deleting
or overwriting files, shell commands, system settings) stay behind it in the
server tools regardless. Opening a file goes through the same `server.file_roots`
sandbox, so N.O.V.A. can only open paths you have allowed.

Settings live under **Desktop**:

- **Enabled** turns the whole skill on or off.
- **Allow launch** governs launching applications specifically; page and file
  opening are separate, so you can leave app-launching off while keeping the rest.
- **App allowlist** — if you list any application names here, only those may be
  launched; leave it empty to allow any installed app.
- **Search URL** is the template a web search opens; `{query}` is replaced with
  your terms (DuckDuckGo by default).

Opening things uses the standard desktop tools — `xdg-open` on Linux, with a
`webbrowser` fallback for URLs. The clipboard needs one of `wl-clipboard`
(Wayland) or `xclip`/`xsel` (X11); without one, N.O.V.A. says so rather than
failing silently. On macOS and Windows the built-in openers and clipboard are
used, so nothing extra is needed.

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

### On-device camera detection

Separate from the cloud description above, N.O.V.A. can *detect* on a local
camera without anything leaving the machine — instant, private, no key. Enable
a camera (**Vision → Camera enabled**, plus a device index) and it can:

- **Detect motion** — "is anything moving in the office", "is the room still".
  Two frames a moment apart are differenced in NumPy on the spot; nothing is
  uploaded. Tune how much movement counts as motion with **Vision → Motion
  sensitivity** (lower is more sensitive).
- **Count people and name them** — "how many people are here", "who's in the
  room". This uses the same local face detection room-watch uses, so it shares
  the `vision` extra and the face models (`python scripts/fetch_models.py
  --only face`), and names anyone you've enrolled.

Motion detection needs only NumPy; the people count needs OpenCV and the face
models, and says so plainly if they're missing rather than failing.

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

### Room-watch: face recognition alerts

"Watch my room" arms a camera to alert on anyone whose face is not one it
recognises: N.O.V.A. speaks a warning immediately, shows a notification, and
pushes an alert straight to your phone with a link to see the camera live.
It is armed and disarmed by voice, never left running by default — partly
because that is what makes sense for "while I'm out", partly because the
camera's own light stays lit the whole time it is watching, which should be
a choice made each time, not a silent permanent state.

It needs the `vision` extra already installed (the same one the local
webcam surface uses) and two small model files, both free, both from
OpenCV's own model zoo:

```bash
python scripts/fetch_models.py --only face
```

1. **Have a named camera set up already** — see **Showing things on
   screen** above. Room-watch reuses whichever entry you already have under
   **Vision → Named cameras**.
2. **Point Security → Camera** at that same name.
3. **Enroll your face**: say something like *"this is my face"* while
   looking at the camera. Say it again from another angle or in different
   lighting any time — it adds to what is recognised rather than replacing
   it.
4. **Arm it**: *"watch my room"* / *"arm the bedroom"*. Say *"stand down"*
   (or similar) to disarm.
5. **Get the phone push working.** The first time you arm, a topic name is
   generated and saved to **Security → Ntfy topic** — read it there, install
   the free [ntfy app](https://ntfy.sh/) on your phone, and subscribe to
   that exact topic name. Anyone who knows the topic name can see what gets
   posted to it, so treat it like a password: don't share it, and if it ever
   leaks, clear the field and arm again to generate a fresh one.
6. **For the tap-through link to actually open the camera**, set
   **Connection → Public URL** to the HTTPS address the mobile web client is
   reachable at (see **Mobile web client** below) — e.g.
   `https://your-machine.your-tailnet.ts.net`. This is deliberately not the
   same as **Connection → Host**: a reverse proxy normally terminates HTTPS
   on a different port than the core itself listens on. Left blank, every
   other channel still fires — spoken warning, in-app notification, phone
   push — just without a link straight to the live view.

Nothing about a face is ever uploaded anywhere: a frame becomes a few
hundred numbers (an embedding) describing it, entirely on this machine, and
that is what gets compared and stored — not a photo. Recognition itself
runs the same OpenCV models `look_at_camera` already depends on
(`opencv-python-headless`), so nothing new needs installing beyond the two
model files above.

**Face detection has real limits.** It only fires when someone is looking at
the camera in decent light; walk in facing away or stand across a dim room and
it sees nothing. For dependable "is a person here" — proper detection of whole
people regardless of orientation — use **Frigate** below instead.

### Frigate: reliable person detection through Home Assistant

If you have a real camera setup, [Frigate](https://frigate.video/) is the
grown-up version of the webcam room-watch above. It runs on-device object
detection (person, car, animal) with its own models and — crucially — it plugs
straight into Home Assistant, so N.O.V.A. reads its results from the same live
entity cache it already uses for lights. No webcam guesswork, no face angle to
catch: Frigate says "person on the driveway", N.O.V.A. acts on it.

Setup:

1. **Run Frigate** and add its official Home Assistant integration (see
   Frigate's own docs). That gives HA a `binary_sensor.<camera>_person` for
   each camera (on while a person is in view) and a person-count sensor.
2. **Turn it on in N.O.V.A.**: **Frigate → Enabled**. That's the only required
   setting — the cameras and sensors are discovered automatically from Home
   Assistant, so there's nothing to point at by hand.
3. **Optional tuning** under **Frigate**: **Object type** (defaults to
   `person`; set `car`, `dog`, etc. to watch for something else), **Cameras**
   (limit alerts to specific camera names; empty = all), **Alert cooldown**,
   and **Alert on detection** (off to keep the query tools but silence the
   pushes).

Then:

- **Ask** — *"is anyone at the front door"*, *"is anyone in the office"*,
  *"who's around"* → answered from Frigate's detection, reliably.
- **Get alerted** — when a person first appears on a camera, N.O.V.A. routes it
  through the same presence logic as everything else: **spoken if you're home,
  pushed to your phone if you're out** (set up the phone push under
  **Notifications** as below). That's the camera alert that actually works.

Frigate needs Home Assistant connected; with it, nothing else is required on
N.O.V.A.'s side — no extra Python packages, no models to download here.

### Reaching you: presence-aware notifications

N.O.V.A. can send you a notification and route it to wherever you actually
are. When it has something to tell you proactively — a reminder, a task that
finished, something worth flagging — it decides between speaking it out loud
and pushing it to your phone based on whether you're in the room:

- **In the room** → it says it aloud (and still leaves a panel on screen).
- **Away** → it pushes to your phone over ntfy, and leaves the panel waiting
  for when you get back.

It works out presence from two signals, so it's useful with or without a
camera:

1. **Recent interaction** — if you've spoken to it in the last few minutes
   (**Presence → Interaction window**), you're obviously here. This needs no
   camera at all.
2. **A camera glance** — only when the interaction signal is cold, it takes a
   *single* frame and checks for your enrolled face (the same faces room-watch
   uses). Turn this off with **Presence → Use camera** to rely on interaction
   alone and never light the camera for a presence check; it also naturally
   piggybacks on room-watch while that's armed, with no extra capture.

Setup is minimal:

- Presence reuses room-watch's enrolled face and camera, so if you've done the
  room-watch steps above, camera presence already works. Otherwise it falls
  back to the interaction signal.
- **Point Presence → Camera** at a named camera if you want a different one
  than room-watch uses; leave it blank to share room-watch's.
- **Phone push** uses its own topic under **Notifications → Push topic**,
  generated and saved the first time N.O.V.A. needs to reach you while you're
  away. Read it there, subscribe to it in the [ntfy app](https://ntfy.sh/),
  and treat it like a password — same as the room-watch topic. Turn push off
  entirely with **Notifications → Push enabled**.

Like everything else, each channel degrades on its own: no voice service, no
push topic or notifications disabled each fail independently, and a notification
is always left on screen so nothing is lost to a wrong guess about where you are.

## Environment overrides

Any setting can be overridden with an environment variable, which is useful for
secrets you would rather not have on disk:

```bash
export NOVA_OPENAI__API_KEY=sk-...
export NOVA_ANTHROPIC__API_KEY=sk-ant-...   # optional advanced reasoning tier
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
