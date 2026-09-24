/**
 * Floating notification panels.
 *
 * Panels animate in, sit for their timeout, and leave. The exit animation is
 * driven by a local `leaving` set rather than by removing the element outright,
 * because an element that disappears mid-transition looks like a glitch.
 *
 * Confirmation prompts are the exception: they have no timeout and must be
 * answered, since they gate a destructive action on the user's machines.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import type { NotificationPayload } from '@protocol';
import { Requests } from '@protocol';
import { pulseCore } from '@/components/NovaCore';
import type { BridgeClient } from '@/lib/bridge';
import { useNova } from '@/state/store';

const ICONS: Record<string, string> = {
  info: 'M12 8h.01M11 12h1v4h1',
  check: 'M5 13l4 4L19 7',
  alert: 'M12 9v4m0 4h.01M10.3 3.9L1.8 18a2 2 0 001.7 3h17a2 2 0 001.7-3L14.7 3.9a2 2 0 00-3.4 0z',
  question: 'M9.1 9a3 3 0 015.8 1c0 2-3 3-3 3M12 17h.01',
  home: 'M3 12l9-9 9 9M5 10v10h14V10',
  server: 'M4 5h16v6H4zM4 13h16v6H4zM8 8h.01M8 16h.01',
  calendar: 'M8 2v4M16 2v4M3 8h18M5 4h14v16H5z',
};

function iconPath(name: string): string {
  return ICONS[name] ?? ICONS.info!;
}

interface PanelProps {
  notification: NotificationPayload;
  leaving: boolean;
  onDismiss: (id: string) => void;
  onRespond: (notification: NotificationPayload, approved: boolean) => void;
}

function Panel({ notification, leaving, onDismiss, onRespond }: PanelProps): JSX.Element {
  const isPrompt = notification.level === 'prompt';

  return (
    <article
      className={`panel panel--${notification.level}${leaving ? ' panel--leaving' : ''}`}
      role={isPrompt ? 'alertdialog' : 'status'}
    >
      <svg className="panel__icon" viewBox="0 0 24 24" aria-hidden="true">
        <path d={iconPath(notification.icon)} />
      </svg>

      <div className="panel__body">
        <h3 className="panel__title">{notification.title}</h3>
        {notification.body && <p className="panel__text">{notification.body}</p>}

        {isPrompt && (
          <div className="panel__actions">
            <button
              type="button"
              className="panel__action panel__action--confirm"
              onClick={() => onRespond(notification, true)}
            >
              Confirm
            </button>
            <button
              type="button"
              className="panel__action"
              onClick={() => onRespond(notification, false)}
            >
              Cancel
            </button>
          </div>
        )}
      </div>

      {!isPrompt && (
        <button
          type="button"
          className="panel__close"
          onClick={() => onDismiss(notification.id)}
          aria-label="Dismiss"
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M6 6l12 12M18 6L6 18" />
          </svg>
        </button>
      )}

      {!isPrompt && notification.timeout > 0 && (
        <div
          className="panel__timer"
          style={{ animationDuration: `${notification.timeout}s` }}
        />
      )}
    </article>
  );
}

export function Notifications({ client }: { client: BridgeClient | null }): JSX.Element {
  const notifications = useNova((store) => store.notifications);
  const dismissNotification = useNova((store) => store.dismissNotification);
  const position = useNova((store) => store.settings?.notifications?.position ?? 'top-right');

  const [leaving, setLeaving] = useState<Set<string>>(new Set());
  const timers = useRef(new Map<string, ReturnType<typeof setTimeout>>());

  const beginExit = useCallback(
    (id: string) => {
      setLeaving((current) => new Set(current).add(id));
      // Matches the panel exit transition in interface.css.
      setTimeout(() => {
        dismissNotification(id);
        setLeaving((current) => {
          const next = new Set(current);
          next.delete(id);
          return next;
        });
      }, 340);
    },
    [dismissNotification],
  );

  const dismiss = useCallback(
    (id: string) => {
      beginExit(id);
      client?.request(Requests.NotificationDismiss, { id }).catch(() => undefined);
    },
    [beginExit, client],
  );

  const respond = useCallback(
    (notification: NotificationPayload, approved: boolean) => {
      beginExit(notification.id);
      const token = notification.token ?? notification.actions?.[0]?.token;
      if (!token || !client) return;
      client.request(Requests.Confirm, { token, approved }).catch((error) => {
        console.error('[nova] confirmation failed', error);
      });
    },
    [beginExit, client],
  );

  // Auto-dismiss on timeout. Prompts are excluded: they must be answered.
  useEffect(() => {
    const active = timers.current;
    for (const notification of notifications) {
      if (active.has(notification.id) || notification.level === 'prompt') continue;
      if (notification.timeout <= 0) continue;
      active.set(
        notification.id,
        setTimeout(() => {
          active.delete(notification.id);
          beginExit(notification.id);
        }, notification.timeout * 1000),
      );
    }
    // Drop timers for panels that are already gone.
    for (const [id, timer] of active) {
      if (!notifications.some((n) => n.id === id)) {
        clearTimeout(timer);
        active.delete(id);
      }
    }
  }, [notifications, beginExit]);

  useEffect(() => {
    const active = timers.current;
    return () => {
      for (const timer of active.values()) clearTimeout(timer);
      active.clear();
    };
  }, []);

  // Every arriving panel makes the Core react, tying the two together.
  const count = notifications.length;
  const previousCount = useRef(0);
  useEffect(() => {
    if (count > previousCount.current) pulseCore(0.5);
    previousCount.current = count;
  }, [count]);

  return (
    <div className={`notifications notifications--${position}`} aria-live="polite">
      {notifications.map((notification) => (
        <Panel
          key={notification.id}
          notification={notification}
          leaving={leaving.has(notification.id)}
          onDismiss={dismiss}
          onRespond={respond}
        />
      ))}
    </div>
  );
}
