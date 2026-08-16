/**
 * Wire protocol shared by the core service and the desktop shell.
 *
 * This is the TypeScript mirror of `nova/transport/protocol.py`. The two are
 * kept in step by `scripts/check_protocol.py`, which fails CI when a topic
 * exists on one side and not the other — the failure mode otherwise is a
 * request that silently routes nowhere.
 */

export const PROTOCOL_VERSION = 1;

export type MessageKind = 'event' | 'request' | 'response' | 'error' | 'hello';

export interface Envelope<T = Record<string, unknown>> {
  v: number;
  kind: MessageKind;
  topic: string;
  id: string;
  ts: number;
  payload: T;
}

/** Requests the shell may send. Mirrors `Requests` in protocol.py. */
export const Requests = {
  Hello: 'hello',
  SettingsGet: 'settings.get',
  SettingsSchema: 'settings.schema',
  SettingsSet: 'settings.set',
  VoiceActivate: 'voice.activate',
  VoiceCancel: 'voice.cancel',
  TextSubmit: 'text.submit',
  VoiceAudioSubmit: 'voice.audio.submit',
  Confirm: 'action.confirm',
  MetricsGet: 'system.metrics.get',
  ServicesGet: 'system.services.get',
  SkillsGet: 'skills.list',
  AudioDevices: 'audio.devices',
  NotificationDismiss: 'notification.dismiss',
  HomeEntities: 'home.entities',
  HomelabStatus: 'homelab.status',
  CalendarAgenda: 'calendar.agenda',
  AppQuit: 'app.quit',
} as const;

export type RequestTopic = (typeof Requests)[keyof typeof Requests];

/** Events the core emits. Mirrors `Topics` in runtime/events.py. */
export const Topics = {
  Ready: 'system.ready',
  Shutdown: 'system.shutdown',
  ServiceHealth: 'system.service.health',
  CapabilityDegraded: 'system.capability.degraded',
  StateChanged: 'state.changed',
  WakeDetected: 'voice.wake.detected',
  ListenStarted: 'voice.listen.started',
  ListenEnded: 'voice.listen.ended',
  AudioLevel: 'voice.audio.level',
  TranscriptPartial: 'voice.transcript.partial',
  TranscriptFinal: 'voice.transcript.final',
  SpeechStarted: 'voice.speech.started',
  SpeechEnded: 'voice.speech.ended',
  TurnStarted: 'assistant.turn.started',
  TurnText: 'assistant.turn.text',
  TurnCompleted: 'assistant.turn.completed',
  TurnFailed: 'assistant.turn.failed',
  ToolStarted: 'assistant.tool.started',
  ToolFinished: 'assistant.tool.finished',
  Notification: 'ui.notification',
  NotificationDismiss: 'ui.notification.dismiss',
  CorePulse: 'ui.core.pulse',
  UiSurfaceShow: 'ui.surface.show',
  UiSurfaceDismiss: 'ui.surface.dismiss',
  Metrics: 'system.metrics',
  SettingsUpdated: 'settings.updated',
  HomeEvent: 'home.event',
  CalendarReminder: 'calendar.reminder',
} as const;

export type EventTopic = (typeof Topics)[keyof typeof Topics];

/** The assistant's visible state. Drives the Core animation. */
export type NovaState =
  | 'booting'
  | 'idle'
  | 'listening'
  | 'thinking'
  | 'speaking'
  | 'error'
  | 'notifying';

export type NotificationLevel = 'info' | 'success' | 'warning' | 'critical' | 'prompt';

export interface NotificationPayload {
  id: string;
  title: string;
  body: string;
  level: NotificationLevel;
  icon: string;
  source: string;
  timeout: number;
  token?: string;
  actions?: { id: string; label: string; token?: string }[];
  createdAt: number;
}

/**
 * What `display.show_map` / `display.show_camera` put on screen — a map
 * centred on a geocoded point, or a camera resolved to a snapshot path the
 * frontend polls on an interval. `streamPath` is relative (e.g.
 * `/camera/ha:camera.front_door`); the frontend appends the bridge's own
 * host and token, since the core never puts a secret in a broadcast payload.
 */
export type SurfacePayload =
  | { kind: 'map'; title: string; lat: number; lon: number }
  | { kind: 'camera'; title: string; streamPath: string };

export interface ServiceHealthPayload {
  name: string;
  state: 'created' | 'starting' | 'running' | 'degraded' | 'stopping' | 'stopped' | 'failed';
  detail: string | null;
  since: number;
  restarts: number;
}

export interface GpuPayload {
  index: number;
  name: string;
  vendor: string;
  utilisation: number;
  memoryUsedMb: number;
  memoryTotalMb: number;
  memoryPercent: number;
  temperatureC: number;
  powerWatts: number;
  fanPercent: number;
}

export interface DiskPayload {
  mountpoint: string;
  device: string;
  totalGb: number;
  usedGb: number;
  percent: number;
}

export interface MetricsPayload {
  timestamp: number;
  cpu: { percent: number; perCore: number[]; loadAverage: number[] };
  memory: { percent: number; usedGb: number; totalGb: number; swapPercent: number };
  disks: DiskPayload[];
  gpus: GpuPayload[];
  temperatures: Record<string, number>;
  network: { sentMbps: number; recvMbps: number };
  processes: number;
  uptimeSeconds: number;
}

export interface StateChangedPayload {
  state: NovaState;
  previous: NovaState;
  reason: string;
}

export interface TranscriptPayload {
  text: string;
  confidence?: number;
  source?: string;
}

export interface TurnTextPayload {
  text: string;
  final: boolean;
}

export interface AudioLevelPayload {
  level: number;
  listening: boolean;
}

/** Descriptor written by the core so the shell can find and authenticate to it. */
export interface BridgeDescriptor {
  host: string;
  port: number;
  token: string;
  pid: number;
  version: number;
  startedAt: number;
}

/** A settings-panel control, derived from the pydantic schema at runtime. */
export interface SettingsField {
  key: string;
  label: string;
  help: string;
  secret: boolean;
  control:
    | 'toggle'
    | 'number'
    | 'slider'
    | 'text'
    | 'textarea'
    | 'password'
    | 'select'
    | 'group'
    | 'list-of-groups'
    | 'string-list';
  options?: { value: string; label: string }[];
  /**
   * When set, the control's options are filled in live by the UI rather than
   * fixed in the schema — e.g. `home_devices` populates a dropdown from the
   * current Home Assistant devices and rooms.
   */
  optionsSource?: string;
  fields?: SettingsField[];
  itemLabelKey?: string | null;
  min?: number;
  max?: number;
  step?: number;
  default?: unknown;
}

export interface SettingsSection {
  key: string;
  label: string;
  description: string;
  fields: SettingsField[];
}

export const REDACTED = '••••••••';

export function isEnvelope(value: unknown): value is Envelope {
  if (typeof value !== 'object' || value === null) return false;
  const candidate = value as Partial<Envelope>;
  return (
    typeof candidate.kind === 'string' &&
    typeof candidate.topic === 'string' &&
    typeof candidate.id === 'string'
  );
}
