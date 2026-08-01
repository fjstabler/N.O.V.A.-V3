# 2. Use Electron for the desktop shell

**Status:** accepted

## Context

The shell must run fullscreen with no browser chrome on both Windows and Ubuntu,
render a demanding WebGL2 scene at 60 FPS, and package into an installable
application. The spec also requires everything except the OpenAI API to be free.

## Decision

Electron, with React and TypeScript, and a hand-written WebGL2 renderer.

## Consequences

**Good.** One GPU stack (Chromium's) behaving identically on both target
platforms, which matters because the Core is the product's centrepiece and a
driver-dependent difference in bloom or blending would be very visible. Mature
packaging via electron-builder. `backgroundThrottling: false` keeps the
animation smooth when the window is not focused.

**Bad.** Roughly 150 MB of runtime and ~100 MB of baseline RAM, against a spec
that asks for low RAM usage. Judged acceptable: this is a fullscreen application
that is the user's primary interface, not a background daemon — and the actual
memory pressure in this system comes from the ML models, not the shell.

## Alternatives considered

**Tauri.** Materially lighter — a few megabytes and far less RAM. Rejected on
platform consistency: on Linux it renders through webkit2gtk, whose WebGL
support varies by distribution and driver, and the Core is the one thing that
cannot be allowed to look different or run slowly on one of the two target
platforms. It also adds a Rust toolchain to the build.

**A native toolkit (Qt, GTK, WinUI).** Best RAM profile, but no shared codebase
across Windows and Linux, and the shader pipeline would be written twice.

## Notes

The renderer is deliberately dependency-free — no three.js. Five passes over a
fullscreen triangle need no scene graph, and writing the GL directly keeps the
bundle small and the pipeline fully under our control. A Canvas2D fallback
covers machines without WebGL2.
