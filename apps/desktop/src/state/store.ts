/**
 * Application state.
 *
 * A single zustand store, fed entirely by bridge events. The renderer reads
 * from it; nothing writes to it except `wireBridge` and explicit user actions,
 * so there is exactly one path by which the UI can change.
 *
 * Note what is *not* here: no conversation history. The spec is explicit that
 * the interface shows only the time and the Core, so transcripts are transient
 * — the current utterance and reply live here and are cleared when the turn
 * ends. Long-term memory is the core's job, not the UI's.
 */

import { create } from 'zustand';
import type {
  MetricsPayload,
  NotificationPayload,
  NovaState,
  ServiceHealthPayload,
  SettingsSection,
  SurfacePayload,
} from '@protocol';
import { Topics } from '@protocol';
import type { BridgeClient, ConnectionState } from '@/lib/bridge';

export interface Settings {
  appearance: {
    theme: string;
    animation_quality: string;
    gpu_acceleration: boolean;
    clock_24_hour: boolean;
    show_seconds: boolean;
    show_date: boolean;
    core_scale: number;
    bloom_intensity: number;
    particle_density: number;
    reduce_motion: boolean;
  };
  developer: {
    debug_mode: boolean;
    show_fps: boolean;
    show_state_badge: boolean;
    text_console: boolean;
  };
  notifications: { position: string; max_visible: number };
  [key: string]: unknown;
}

export interface CapabilityNotice {
  capability: string;
  message: string;
  remedy: string | null;
}

interface NovaStore {
  connection: ConnectionState;
  state: NovaState;
  level: number;
  /** Timestamp of the last time the assistant was actively doing something
   *  for the user — reset on every state change away from idle, and by
   *  anything else that counts as "someone just interacted" (see
   *  `pulseCore`). Drives the Core's quiet-resting look after a long
   *  stretch with nobody talking to it. */
  lastActiveAt: number;

  /** What the user is currently saying, or just said. */
  transcript: string;
  /** What N.O.V.A. is currently saying, streamed token by token. */
  reply: string;
  /** Tool currently running, shown as a subtle status line. */
  activeTool: string | null;

  notifications: NotificationPayload[];
  metrics: MetricsPayload | null;
  services: ServiceHealthPayload[];
  degraded: CapabilityNotice[];

  /** What `display.show_map` / `show_camera` put on screen — not conversation
   * history, a deliberate visual answer to something that was asked for. */
  surface: SurfacePayload | null;

  settings: Settings | null;
  settingsSchema: SettingsSection[];

  settingsOpen: boolean;
  consoleOpen: boolean;
  diagnosticsOpen: boolean;
  fps: number;

  setConnection: (connection: ConnectionState) => void;
  setState: (state: NovaState) => void;
  setLevel: (level: number) => void;
  noteInteraction: () => void;
  setTranscript: (text: string) => void;
  appendReply: (text: string) => void;
  setReply: (text: string) => void;
  clearConversation: () => void;
  setActiveTool: (tool: string | null) => void;

  pushNotification: (notification: NotificationPayload) => void;
  dismissNotification: (id: string) => void;

  showSurface: (surface: SurfacePayload) => void;
  dismissSurface: () => void;

  setMetrics: (metrics: MetricsPayload) => void;
  upsertService: (service: ServiceHealthPayload) => void;
  setServices: (services: ServiceHealthPayload[]) => void;
  addDegraded: (notice: CapabilityNotice) => void;

  setSettings: (settings: Settings) => void;
  setSettingsSchema: (sections: SettingsSection[]) => void;

  toggleSettings: (open?: boolean) => void;
  toggleConsole: (open?: boolean) => void;
  toggleDiagnostics: (open?: boolean) => void;
  setFps: (fps: number) => void;
}

export const useNova = create<NovaStore>((set) => ({
  connection: 'connecting',
  state: 'booting',
  level: 0,
  lastActiveAt: Date.now(),
  transcript: '',
  reply: '',
  activeTool: null,
  notifications: [],
  metrics: null,
  services: [],
  degraded: [],
  surface: null,
  settings: null,
  settingsSchema: [],
  settingsOpen: false,
  consoleOpen: false,
  diagnosticsOpen: false,
  fps: 0,

  setConnection: (connection) => set({ connection }),
  noteInteraction: () => set({ lastActiveAt: Date.now() }),
  setState: (state) => set({ state }),
  setLevel: (level) => set({ level }),
  setTranscript: (transcript) => set({ transcript }),
  appendReply: (text) => set((current) => ({ reply: current.reply + text })),
  setReply: (reply) => set({ reply }),
  clearConversation: () => set({ transcript: '', reply: '', activeTool: null }),
  setActiveTool: (activeTool) => set({ activeTool }),

  pushNotification: (notification) =>
    set((current) => ({
      // Cap the stack; the core also limits it, but a burst during a reconnect
      // could otherwise arrive all at once.
      notifications: [...current.notifications.filter((n) => n.id !== notification.id), notification].slice(-6),
    })),
  dismissNotification: (id) =>
    set((current) => ({ notifications: current.notifications.filter((n) => n.id !== id) })),

  showSurface: (surface) => set({ surface }),
  dismissSurface: () => set({ surface: null }),

  setMetrics: (metrics) => set({ metrics }),
  upsertService: (service) =>
    set((current) => ({
      services: [...current.services.filter((s) => s.name !== service.name), service].sort((a, b) =>
        a.name.localeCompare(b.name),
      ),
    })),
  setServices: (services) => set({ services }),
  addDegraded: (notice) =>
    set((current) =>
      current.degraded.some((d) => d.capability === notice.capability)
        ? current
        : { degraded: [...current.degraded, notice] },
    ),

  setSettings: (settings) => set({ settings }),
  setSettingsSchema: (settingsSchema) => set({ settingsSchema }),

  toggleSettings: (open) =>
    set((current) => ({ settingsOpen: open ?? !current.settingsOpen, consoleOpen: false })),
  toggleConsole: (open) => set((current) => ({ consoleOpen: open ?? !current.consoleOpen })),
  toggleDiagnostics: (open) =>
    set((current) => ({ diagnosticsOpen: open ?? !current.diagnosticsOpen })),
  setFps: (fps) => set({ fps }),
}));

/**
 * Subscribe the store to the bridge.
 *
 * Returns a teardown function. Every core event that the UI reacts to is wired
 * here and nowhere else, which keeps the components free of transport concerns.
 */
export function wireBridge(client: BridgeClient): () => void {
  const unsubscribes: (() => void)[] = [];

  unsubscribes.push(client.onStateChange((connection) => useNova.getState().setConnection(connection)));

  unsubscribes.push(
    client.on(Topics.StateChanged, (payload) => {
      const next = payload.state as NovaState;
      const current = useNova.getState();
      current.setState(next);
      // Anything other than settling into idle is the assistant actively
      // doing something for the user — the signal the Core's idle-dimmed
      // look watches for to know it should wake back up.
      if (next !== 'idle') current.noteInteraction();
      // Returning to idle ends the turn; the interface goes quiet again.
      if (next === 'idle') {
        setTimeout(() => {
          const latest = useNova.getState();
          if (latest.state === 'idle') latest.clearConversation();
        }, 4000);
      }
    }),
  );

  unsubscribes.push(
    client.on(Topics.AudioLevel, (payload) => {
      useNova.getState().setLevel(Number(payload.level ?? 0));
    }),
  );

  unsubscribes.push(
    client.on(Topics.TranscriptFinal, (payload) => {
      const current = useNova.getState();
      current.setTranscript(String(payload.text ?? ''));
      current.setReply('');
    }),
  );

  unsubscribes.push(
    client.on(Topics.TurnText, (payload) => {
      const current = useNova.getState();
      const text = String(payload.text ?? '');
      // `final` with text replaces (a non-streamed reply); without text it just
      // marks the end of a stream that has already been appended.
      if (payload.final === true) {
        if (text) current.setReply(text);
      } else if (text) {
        current.appendReply(text);
      }
    }),
  );

  unsubscribes.push(
    client.on(Topics.ToolStarted, (payload) => {
      useNova.getState().setActiveTool(String(payload.tool ?? ''));
    }),
  );
  unsubscribes.push(client.on(Topics.ToolFinished, () => useNova.getState().setActiveTool(null)));

  unsubscribes.push(
    client.on(Topics.TurnFailed, (payload) => {
      useNova.getState().setReply(String(payload.error ?? 'Something went wrong.'));
    }),
  );

  unsubscribes.push(
    client.on(Topics.Notification, (payload) => {
      // The core re-publishes routed notifications with an id; the raw ones
      // that subsystems emit are filtered out by the notification service.
      if (typeof payload.id !== 'string') return;
      useNova.getState().pushNotification(payload as unknown as NotificationPayload);
    }),
  );

  unsubscribes.push(
    client.on(Topics.NotificationDismiss, (payload) => {
      if (typeof payload.id === 'string') useNova.getState().dismissNotification(payload.id);
    }),
  );

  unsubscribes.push(
    client.on(Topics.UiSurfaceShow, (payload) => {
      useNova.getState().showSurface(payload as unknown as SurfacePayload);
    }),
  );
  unsubscribes.push(client.on(Topics.UiSurfaceDismiss, () => useNova.getState().dismissSurface()));

  unsubscribes.push(
    client.on(Topics.Metrics, (payload) => {
      useNova.getState().setMetrics(payload as unknown as MetricsPayload);
    }),
  );

  unsubscribes.push(
    client.on(Topics.ServiceHealth, (payload) => {
      useNova.getState().upsertService(payload as unknown as ServiceHealthPayload);
    }),
  );

  unsubscribes.push(
    client.on(Topics.CapabilityDegraded, (payload) => {
      useNova.getState().addDegraded({
        capability: String(payload.capability ?? 'unknown'),
        message: String(payload.message ?? ''),
        remedy: (payload.remedy as string | null) ?? null,
      });
    }),
  );

  unsubscribes.push(
    client.on(Topics.SettingsUpdated, (payload) => {
      if (payload.settings) useNova.getState().setSettings(payload.settings as Settings);
    }),
  );

  // On (re)connect, pull everything that is state rather than event.
  unsubscribes.push(
    client.onStateChange(async (connection) => {
      if (connection !== 'connected') return;
      try {
        const [settings, schema, services] = await Promise.all([
          client.request<{ settings: Settings }>('settings.get'),
          client.request<{ sections: SettingsSection[] }>('settings.schema'),
          client.request<{ services: ServiceHealthPayload[] }>('system.services.get'),
        ]);
        const current = useNova.getState();
        current.setSettings(settings.settings);
        current.setSettingsSchema(schema.sections);
        current.setServices(services.services);
      } catch (error) {
        console.error('[nova] initial sync failed', error);
      }
    }),
  );

  return () => {
    for (const unsubscribe of unsubscribes) unsubscribe();
  };
}
