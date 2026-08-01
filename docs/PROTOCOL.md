# Wire protocol

The core service and the desktop shell speak a small JSON protocol over a
loopback WebSocket.

## Transport

```
ws://127.0.0.1:8765/?token=<shared-secret>
```

Bound to loopback and gated on a token generated on first run. That combination
stops any other process on the machine — or a page in a stray browser — from
driving the assistant.

The shell finds the port and token in a **bridge descriptor** the core writes on
startup:

```jsonc
// ~/.local/share/nova/bridge.json, mode 0600
{ "host": "127.0.0.1", "port": 8765, "token": "…", "pid": 4711,
  "version": 1, "startedAt": 1785535780.4 }
```

The same JSON is echoed on stdout prefixed with `NOVA_BRIDGE_READY `, so the
shell works whether it spawned the core as a child or attached to one already
running as a system service.

## Envelope

One shape carries every message:

```jsonc
{
  "v": 1,                    // protocol version; a mismatch is dropped and logged
  "kind": "event",           // event | request | response | error | hello
  "topic": "state.changed",
  "id": "9f3c…",             // correlates request → response/error
  "ts": 1785535780.42,
  "payload": { }
}
```

- `event` — one-way, core → shell.
- `request` → `response` | `error` — shell → core, correlated by `id`.
- `hello` — sent by the core immediately on connect, carrying the current state,
  the available routes and the service health report.

Requests time out client-side after 20 seconds, so a UI action can never hang
forever waiting on a reply that will not arrive.

## Requests

| Topic | Payload | Returns |
| --- | --- | --- |
| `hello` | — | version, state, services, voice status |
| `settings.get` | — | `{ settings }` with secrets redacted |
| `settings.schema` | — | `{ sections }` — the panel's layout |
| `settings.set` | `{ patch }` | `{ settings }` |
| `voice.activate` | — | `{ activated }` |
| `voice.cancel` | — | `{ cancelled }` |
| `text.submit` | `{ text }` | `{ text, tools, error, ms }` |
| `action.confirm` | `{ token, approved }` | `{ text }` or `{ cancelled }` |
| `system.metrics.get` | — | host info + latest metrics |
| `system.services.get` | — | `{ services }` |
| `skills.list` | — | `{ skills }` with their tools |
| `audio.devices` | — | `{ devices, voice }` |
| `notification.dismiss` | `{ id }` | `{ dismissed }` |
| `home.entities` | `{ domain? }` | `{ entities, connected }` |
| `homelab.status` | — | `{ services }` |
| `calendar.agenda` | `{ days? }` | `{ events }` |
| `app.quit` | — | `{ stopping }` |

Anything not in this table is rejected with `nova.unknown_route`. The table is
the complete UI-facing surface, which makes it auditable at a glance.

## Events

| Topic | When |
| --- | --- |
| `system.ready` | Boot complete |
| `system.service.health` | A service changes state |
| `system.capability.degraded` | An optional capability is unavailable |
| `state.changed` | The assistant state machine moves |
| `voice.wake.detected` | Wake phrase fired |
| `voice.listen.started` / `.ended` | Utterance capture |
| `voice.audio.level` | Microphone loudness (throttled to 30 Hz) |
| `voice.transcript.final` | A finished transcription |
| `voice.speech.started` / `.ended` | Synthesis playback |
| `assistant.turn.started` / `.text` / `.completed` / `.failed` | Turn lifecycle |
| `assistant.tool.started` / `.finished` | Tool execution |
| `ui.notification` / `ui.notification.dismiss` | Notification panels |
| `system.metrics` | Host telemetry (throttled to 1 Hz) |
| `settings.updated` | Settings changed |
| `home.event` | Home Assistant state change or MQTT message |
| `calendar.reminder` | An event is approaching |

High-frequency topics are coalesced server-side so a 60 FPS UI is never
back-pressured by a 100 Hz producer.

## Errors

```jsonc
{ "v": 1, "kind": "error", "topic": "voice.activate", "id": "…",
  "payload": { "code": "nova.capability.unavailable", "message": "no microphone" } }
```

Codes are stable and machine-readable:

| Code | Meaning |
| --- | --- |
| `nova.config` | Invalid settings patch |
| `nova.capability.unavailable` | A subsystem is degraded |
| `nova.capability.missing_dependency` | An optional package is not installed |
| `nova.capability.missing_model` | A local model file is absent |
| `nova.confirmation_required` | A destructive action needs approval |
| `nova.permission_denied` | Refused by the policy engine |
| `nova.skill.tool` | A tool failed |
| `nova.unknown_route` | No such request topic |
| `nova.internal` | Unhandled — a bug |

`nova.confirmation_required` carries `action`, `summary` and `token`; reply with
`action.confirm`.

## Keeping the two definitions in step

The protocol is declared twice — `nova/transport/protocol.py` and
`packages/protocol/src/index.ts` — so neither side needs the other's toolchain.
Two declarations can drift, and the failure is silent: a request that routes
nowhere.

`scripts/check_protocol.py` compares them and fails CI on any difference. Run it
after touching either side:

```bash
make protocol
```

## Versioning

`v` is bumped when a change is not backwards compatible. Both sides drop
messages whose version they do not recognise and log a mismatch, so an old shell
against a new core fails loudly at the transport rather than subtly in a
handler.
