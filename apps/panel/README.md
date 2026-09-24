# N.O.V.A. panel

The Android build. A WebView holding the same interface the desktop shell runs
— same Core animation, same surfaces, same settings panel — plus a foreground
service that lends the device's microphone and speaker to the core.

Built for an Echo Show 5 running LineageOS, which is where it was designed, but
nothing in it is specific to that device: any Android 8+ screen works.

## What runs where

The panel is a client, not a second brain. Wake word, endpointing, Whisper and
Kokoro all stay on the core, along with every skill, the memory database and
the settings. The panel contributes a screen, a microphone and a speaker.

That is what makes a panel and a phone and the desktop shell the same
assistant, with one wake phrase and one set of settings behind them, rather
than three things that each need configuring.

```
Echo Show                              the core (PC, Proxmox LXC, …)
┌──────────────────────────┐           ┌────────────────────────────────┐
│ PanelActivity  (WebView) │──── ws ──▶│ bridge :8765                   │
│   the interface, at /app/│           │                                │
│                          │           │ wake word → endpointer →       │
│ AudioService (native)    │──── ws ──▶│ Whisper → skills → Kokoro      │
│   mic  ▶ 16 kHz frames   │           │                                │
│   spkr ◀ synthesised WAV │◀──────────│                                │
└──────────────────────────┘           └────────────────────────────────┘
```

Audio is captured natively rather than in the WebView for two reasons.
`getUserMedia` requires a secure origin and the core serves plain HTTP on a
private address; and a foreground service keeps running when the screen goes
off, where a WebView is throttled. The page is therefore loaded with `audio=0`
and never asks for a microphone.

## Setting it up

### 1. Let the core listen on the network

By default the bridge binds to loopback, which nothing else can reach. In
N.O.V.A.'s settings, under **Connection**, set the host to either:

- `0.0.0.0` — reachable from the LAN, or
- the machine's Tailscale address — reachable from anywhere on the tailnet,
  which is what you want if the panel is not on the same Wi-Fi.

Restart the core. The token is unchanged either way; the network is the only
thing that moves.

### 2. Get the APK

Either download `nova-panel-debug-apk` from the CI run for your commit, or
build it:

```sh
cd apps/panel
./gradlew assembleDebug
# app/build/outputs/apk/debug/app-debug.apk
```

### 3. Install it

```sh
adb connect <panel-ip>:5555      # or plug in over USB
adb install -r app-debug.apk
```

### 4. Pair it

The panel asks for three things on first launch:

- **Host** — the address from step 1, as the panel sees it
- **Port** — `8765` unless you changed it
- **Token** — from `~/.local/share/nova/bridge.json` on the core machine

Then grant the microphone permission when asked.

### 5. Make it a kiosk

The app already claims `HOME`, so making it the default launcher is what turns
it into an appliance: the home button returns to N.O.V.A., and so does a
reboot or a crash.

```sh
adb shell cmd package set-home-activity com.nova.panel/.PanelActivity
```

Or: **Settings → Apps → Default apps → Home app → N.O.V.A.**

To get back out afterwards, **tap the top-left corner five times** — that opens
the panel's own settings, which is the only route in once there is no
navigation bar. `adb shell cmd package set-home-activity <other>` undoes it.

## Notes

**Two connections, deliberately.** The WebView and the audio service each hold
their own WebSocket. Relaying audio through the page would put eight messages a
second on the UI thread and stop the moment the WebView was reloaded or
throttled — and audio is the one thing here that must not pause.

**Only one panel should hold the microphone.** The core has a single audio
source; a second panel that attaches takes it over. Turn the microphone switch
off on any panel that should only be a display.

**Cleartext HTTP is allowed on purpose.** See
`res/xml/network_security_config.xml`. The bridge is gated on a bearer token
and the boundary is the LAN or the tailnet. Put the core behind TLS and you can
delete that file; the app works over `https`/`wss` unchanged.

**The screen stays on.** `FLAG_KEEP_SCREEN_ON` plus a partial wake lock while
the audio service runs. A panel that has to be woken before it will listen is
not a panel.
