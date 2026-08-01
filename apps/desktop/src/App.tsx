/**
 * The root.
 *
 * When idle, this renders exactly two things: the clock and the Core. Everything
 * else — settings, notifications, the console — is summoned and then returns
 * whence it came. That restraint is the design, not an unfinished state.
 */

import { useEffect, useRef, useState } from 'react';
import { Requests } from '@protocol';
import { Clock } from '@/components/Clock';
import { Conversation } from '@/components/Conversation';
import { Notifications } from '@/components/Notifications';
import { NovaCore } from '@/components/NovaCore';
import { SettingsPanel } from '@/components/SettingsPanel';
import { ConnectionStatus, DeveloperReadout } from '@/components/StatusOverlay';
import { TextConsole } from '@/components/TextConsole';
import { BridgeClient, createDescriptorResolver } from '@/lib/bridge';
import { useNova, wireBridge } from '@/state/store';

export function App(): JSX.Element {
  const [client, setClient] = useState<BridgeClient | null>(null);
  const clientRef = useRef<BridgeClient | null>(null);
  const theme = useNova((store) => store.settings?.appearance?.theme ?? 'nova-blue');
  const state = useNova((store) => store.state);

  // One bridge for the lifetime of the window.
  useEffect(() => {
    const bridge = new BridgeClient(createDescriptorResolver());
    clientRef.current = bridge;
    const unwire = wireBridge(bridge);
    void bridge.connect();
    setClient(bridge);

    // Electron tells us the moment the core is listening, which is faster than
    // waiting for the next reconnect backoff to expire.
    const offBridgeReady = window.nova?.onBridgeReady(() => void bridge.connect());
    const offCoreExited = window.nova?.onCoreExited(() => void bridge.connect());

    return () => {
      offBridgeReady?.();
      offCoreExited?.();
      unwire();
      bridge.close();
      clientRef.current = null;
    };
  }, []);

  // Global shortcuts relayed from the main process, plus their in-window twins
  // so the app behaves the same when run in a browser during development.
  useEffect(() => {
    const store = useNova.getState;

    const activateVoice = () => {
      clientRef.current?.request(Requests.VoiceActivate).catch(() => undefined);
    };
    const toggleSettings = () => store().toggleSettings();
    const toggleConsole = () => store().toggleConsole();

    const offVoice = window.nova?.onActivateVoice(activateVoice);
    const offSettings = window.nova?.onToggleSettings(toggleSettings);
    const offConsole = window.nova?.onToggleConsole(toggleConsole);

    const onKey = (event: KeyboardEvent) => {
      const modifier = event.ctrlKey || event.metaKey;
      if (modifier && event.shiftKey && event.code === 'Space') {
        event.preventDefault();
        activateVoice();
      } else if (modifier && event.key === ',') {
        event.preventDefault();
        toggleSettings();
      } else if (modifier && event.shiftKey && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        toggleConsole();
      } else if (event.key === 'Escape') {
        // Escape always means "stop what you are doing".
        clientRef.current?.request(Requests.VoiceCancel).catch(() => undefined);
      }
    };

    window.addEventListener('keydown', onKey);
    return () => {
      offVoice?.();
      offSettings?.();
      offConsole?.();
      window.removeEventListener('keydown', onKey);
    };
  }, []);

  // The theme is a data attribute so CSS custom properties can cascade from it,
  // keeping colour in one place rather than threaded through components.
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  useEffect(() => {
    document.documentElement.dataset.state = state;
  }, [state]);

  return (
    <main className="stage">
      <NovaCore />

      <div className="stage__content">
        <Clock />
        <Conversation />
      </div>

      <Notifications client={client} />
      <SettingsPanel client={client} />
      <TextConsole client={client} />

      <ConnectionStatus />
      <DeveloperReadout />

      {/* The only affordance on the idle screen, and it stays nearly invisible
          until the pointer finds it. */}
      <button
        type="button"
        className="settings-trigger"
        onClick={() => useNova.getState().toggleSettings()}
        aria-label="Settings"
      >
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <circle cx="12" cy="12" r="3" />
          <path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 11-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 11-4 0v-.09A1.65 1.65 0 008 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 11-2.83-2.83l.06-.06a1.65 1.65 0 00.33-1.82 1.65 1.65 0 00-1.51-1H2a2 2 0 110-4h.09A1.65 1.65 0 004.6 8a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 112.83-2.83l.06.06A1.65 1.65 0 009 3.6 1.65 1.65 0 0010 2.09V2a2 2 0 114 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 112.83 2.83l-.06.06a1.65 1.65 0 00-.33 1.82V9a1.65 1.65 0 001.51 1H22a2 2 0 110 4h-.09a1.65 1.65 0 00-1.51 1z" />
        </svg>
      </button>
    </main>
  );
}
