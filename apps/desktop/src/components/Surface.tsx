/**
 * The surface: whatever N.O.V.A. is putting on screen right now alongside its
 * spoken reply — a map, a camera, your week's agenda, the weather, or a home
 * overview. Each `kind` is a small self-contained view; the shape it needs
 * arrives in the `ui.surface.show` payload from the core.
 *
 * A camera is a snapshot fetched again on every frame, not a persistent video
 * stream — the bridge serves one JPEG per request (see transport/server.py's
 * `_camera_response`). Nothing here pretends to be a video call, but the next
 * request fires the moment the current image finishes loading (or fails)
 * rather than on a fixed timer.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import type {
  AgendaDayPayload,
  AgendaEventPayload,
  HomeOverviewCameraPayload,
  HomeOverviewDevicePayload,
  WeatherDayPayload,
  WeatherHourPayload,
} from '@protocol';
import { pulseCore } from '@/components/NovaCore';
import type { BridgeClient } from '@/lib/bridge';
import { useNova } from '@/state/store';

//: A floor between frames so a very fast local camera cannot spam requests.
const MIN_FRAME_GAP_MS = 120;
//: Auto-dismiss so a camera left open does not poll forever after being forgotten.
const AUTO_DISMISS_MS = 90_000;

function CameraView({ client, streamPath }: { client: BridgeClient; streamPath: string }): JSX.Element {
  const [src, setSrc] = useState<string | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const requestFrame = useCallback(() => {
    const url = client.resourceUrl(streamPath);
    if (url) setSrc(`${url}&t=${Date.now()}`);
  }, [client, streamPath]);

  useEffect(() => {
    requestFrame();
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [requestFrame]);

  const scheduleNext = () => {
    timerRef.current = setTimeout(requestFrame, MIN_FRAME_GAP_MS);
  };

  if (!src) return <div className="surface__placeholder">Connecting…</div>;
  return (
    <img
      className="surface__image"
      src={src}
      alt=""
      onLoad={scheduleNext}
      onError={scheduleNext}
    />
  );
}

function MapView({ lat, lon }: { lat: number; lon: number }): JSX.Element {
  // A small bounding box around the point; no API key, OSM's own embed export.
  const span = 0.02;
  const bbox = `${lon - span},${lat - span},${lon + span},${lat + span}`;
  const src = `https://www.openstreetmap.org/export/embed.html?bbox=${bbox}&marker=${lat},${lon}&layer=mapnik`;
  return <iframe className="surface__frame" src={src} title="Map" loading="lazy" />;
}

// -------------------------------------------------------------- agenda

function formatEventTime(event: AgendaEventPayload): string {
  if (event.allDay) return 'all day';
  const start = new Date(event.startsAt * 1000);
  const end = new Date(event.endsAt * 1000);
  const fmt = (d: Date) =>
    d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', hour12: false });
  return end.getTime() > start.getTime() ? `${fmt(start)} – ${fmt(end)}` : fmt(start);
}

/** Turn HH:MM (24h) into a fractional-hour position on the day column. */
function hourFraction(seconds: number): number {
  const d = new Date(seconds * 1000);
  return d.getHours() + d.getMinutes() / 60;
}

/** A palette so each event gets a stable, distinguishable colour. */
const EVENT_COLOURS = ['#5aa8ff', '#7ed48b', '#ffb86b', '#c99cff', '#ff8fa3', '#5be3d3', '#f6d167'];
function colourFor(summary: string): string {
  let hash = 0;
  for (const ch of summary) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  return EVENT_COLOURS[hash % EVENT_COLOURS.length] ?? '#5aa8ff';
}

function AgendaView({ days }: { days: AgendaDayPayload[] }): JSX.Element {
  // If it's a single day, the strip is overkill — fall back to the tidy list.
  if (days.length <= 1) {
    return <AgendaList days={days} />;
  }
  const isToday = (iso: string) => iso === new Date().toISOString().slice(0, 10);
  const HOURS = [6, 9, 12, 15, 18, 21]; // grid rows
  const startHour = 6;
  const endHour = 24;
  const totalHours = endHour - startHour;
  return (
    <div className="surface__week">
      <div className="week__hours" aria-hidden="true">
        {HOURS.map((h) => (
          <div key={h} className="week__hour-label" style={{ top: `${((h - startHour) / totalHours) * 100}%` }}>
            {String(h).padStart(2, '0')}:00
          </div>
        ))}
      </div>
      <div className="week__columns">
        {days.map((day) => (
          <div key={day.date} className={`week__col${isToday(day.date) ? ' is-today' : ''}`}>
            <div className="week__col-head">
              <div className="week__dow">{day.label.split(' ')[0] ?? ''}</div>
              <div className="week__date">{day.label.split(' ').slice(1).join(' ')}</div>
            </div>
            <div className="week__col-body">
              {HOURS.map((h) => (
                <div
                  key={h}
                  className="week__gridline"
                  style={{ top: `${((h - startHour) / totalHours) * 100}%` }}
                />
              ))}
              {day.events.map((event, index) => {
                if (event.allDay) {
                  return (
                    <div key={index} className="week__all-day" style={{ background: colourFor(event.summary) }}>
                      {event.summary}
                    </div>
                  );
                }
                const start = Math.max(startHour, hourFraction(event.startsAt));
                const end = Math.max(start + 0.5, hourFraction(event.endsAt));
                const top = ((start - startHour) / totalHours) * 100;
                const height = ((end - start) / totalHours) * 100;
                return (
                  <div
                    key={index}
                    className="week__event"
                    style={{
                      top: `${top}%`,
                      height: `${height}%`,
                      background: colourFor(event.summary),
                    }}
                    title={`${event.summary}${event.location ? ` · ${event.location}` : ''}`}
                  >
                    <div className="week__event-title">{event.summary}</div>
                    <div className="week__event-time">{formatEventTime(event)}</div>
                  </div>
                );
              })}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function AgendaList({ days }: { days: AgendaDayPayload[] }): JSX.Element {
  return (
    <div className="surface__agenda">
      {days.map((day) => (
        <div key={day.date} className="agenda-day">
          <h3 className="agenda-day__label">{day.label}</h3>
          {day.events.length === 0 ? (
            <p className="agenda-day__empty">Nothing scheduled</p>
          ) : (
            <ul className="agenda-day__list">
              {day.events.map((event, index) => (
                <li key={`${day.date}-${index}`} className="agenda-event">
                  <span
                    className="agenda-event__swatch"
                    style={{ background: colourFor(event.summary) }}
                    aria-hidden="true"
                  />
                  <span className="agenda-event__time">{formatEventTime(event)}</span>
                  <span className="agenda-event__title">{event.summary}</span>
                  {event.location && (
                    <span className="agenda-event__where">{event.location}</span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      ))}
    </div>
  );
}

// -------------------------------------------------------------- weather

//: WMO code → an emoji glyph, so the surface is legible at a glance.
const WEATHER_ICONS: Record<number, string> = {
  0: '☀️',
  1: '🌤',
  2: '⛅',
  3: '☁️',
  45: '🌫',
  48: '🌫',
  51: '🌦',
  53: '🌦',
  55: '🌧',
  61: '🌧',
  63: '🌧',
  65: '🌧',
  71: '🌨',
  73: '🌨',
  75: '❄️',
  80: '🌦',
  81: '🌧',
  82: '⛈',
  95: '⛈',
  96: '⛈',
  99: '⛈',
};
const weatherIcon = (code: number) => WEATHER_ICONS[code] ?? '🌡';

function WeatherView({
  place,
  unit,
  current,
  hours,
  days,
}: {
  place: string;
  unit: 'C' | 'F';
  current: { temperature: number; feelsLike?: number; code: number; wind?: number };
  hours: WeatherHourPayload[];
  days: WeatherDayPayload[];
}): JSX.Element {
  const hourFmt = (iso: string) =>
    new Date(iso).toLocaleTimeString(undefined, { hour: '2-digit', hour12: false });
  return (
    <div className="surface__weather">
      <div className="weather-now">
        <span className="weather-now__icon" aria-hidden="true">
          {weatherIcon(current.code)}
        </span>
        <div className="weather-now__figures">
          <div className="weather-now__temp">
            {Math.round(current.temperature)}°{unit}
          </div>
          <div className="weather-now__place">{place}</div>
          {current.feelsLike !== undefined && (
            <div className="weather-now__meta">
              Feels {Math.round(current.feelsLike)}°
              {current.wind !== undefined && ` · Wind ${Math.round(current.wind)}`}
            </div>
          )}
        </div>
      </div>
      {hours.length > 0 && (
        <div className="weather-strip" aria-label="Next hours">
          {hours.map((h) => (
            <div key={h.time} className="weather-strip__cell">
              <div className="weather-strip__when">{hourFmt(h.time)}</div>
              <div className="weather-strip__icon">{weatherIcon(h.code)}</div>
              <div className="weather-strip__temp">{Math.round(h.temperature)}°</div>
            </div>
          ))}
        </div>
      )}
      {days.length > 0 && (
        <div className="weather-days">
          {days.map((d) => (
            <div key={d.date} className="weather-days__row">
              <span className="weather-days__label">{d.label}</span>
              <span className="weather-days__icon">{weatherIcon(d.code)}</span>
              <span className="weather-days__hi">{Math.round(d.high)}°</span>
              <span className="weather-days__lo">{Math.round(d.low)}°</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------ overview

/** Emoji glyph per HA domain — the surface reads at a glance. */
const DOMAIN_ICONS: Record<string, string> = {
  light: '💡',
  switch: '🔌',
  fan: '🌀',
  media_player: '🔊',
  climate: '🌡',
  cover: '🪟',
  lock: '🔒',
  camera: '📷',
};
const iconFor = (domain?: string) => (domain && DOMAIN_ICONS[domain]) || '•';

interface DeviceTileProps {
  item: HomeOverviewDevicePayload;
  tone?: 'on' | 'warn';
}
function DeviceTile({ item, tone = 'on' }: DeviceTileProps): JSX.Element {
  return (
    <div className={`device-tile device-tile--${tone}`}>
      <span className="device-tile__icon" aria-hidden="true">
        {iconFor(item.domain)}
      </span>
      <div className="device-tile__body">
        <div className="device-tile__name">{item.name}</div>
        {(item.area || item.detail) && (
          <div className="device-tile__meta">
            {item.area}
            {item.area && item.detail ? ' · ' : ''}
            {item.detail}
          </div>
        )}
      </div>
    </div>
  );
}

function groupByRoom(items: HomeOverviewDevicePayload[]): [string, HomeOverviewDevicePayload[]][] {
  const rooms = new Map<string, HomeOverviewDevicePayload[]>();
  for (const item of items) {
    const key = item.area || 'Elsewhere';
    if (!rooms.has(key)) rooms.set(key, []);
    rooms.get(key)!.push(item);
  }
  return Array.from(rooms.entries()).sort(([a], [b]) => a.localeCompare(b));
}

function CameraThumb({
  client,
  camera,
}: {
  client: BridgeClient;
  camera: HomeOverviewCameraPayload;
}): JSX.Element {
  const [src, setSrc] = useState<string | null>(null);
  useEffect(() => {
    const url = client.resourceUrl(camera.streamPath);
    if (url) setSrc(`${url}&t=${Date.now()}`);
    // Refresh once every 5s — this is a thumbnail, not a live feed.
    const timer = setInterval(() => {
      const next = client.resourceUrl(camera.streamPath);
      if (next) setSrc(`${next}&t=${Date.now()}`);
    }, 5000);
    return () => clearInterval(timer);
  }, [client, camera.streamPath]);
  return (
    <figure className="camera-thumb">
      {src ? <img src={src} alt="" /> : <div className="camera-thumb__loading">·</div>}
      <figcaption>
        <span aria-hidden="true">📷</span> {camera.name}
      </figcaption>
    </figure>
  );
}

function HomeOverviewView({
  client,
  on,
  climate,
  open,
  unlocked,
  cameras,
}: {
  client: BridgeClient;
  on: HomeOverviewDevicePayload[];
  climate: HomeOverviewDevicePayload[];
  open: HomeOverviewDevicePayload[];
  unlocked: HomeOverviewDevicePayload[];
  cameras: HomeOverviewCameraPayload[];
}): JSX.Element {
  const empty =
    on.length + climate.length + open.length + unlocked.length + cameras.length === 0;
  return (
    <div className="surface__overview">
      {empty ? (
        <p className="overview-empty">The house is quiet — nothing on, everything closed.</p>
      ) : (
        <>
          {(open.length > 0 || unlocked.length > 0) && (
            <section className="overview-alert">
              <h3 className="overview-alert__heading">Worth checking</h3>
              <div className="overview-tiles">
                {open.map((item, i) => <DeviceTile key={`open-${i}`} item={item} tone="warn" />)}
                {unlocked.map((item, i) => <DeviceTile key={`lock-${i}`} item={item} tone="warn" />)}
              </div>
            </section>
          )}
          {on.length > 0 && (
            <section>
              <h3 className="overview-heading">
                On <span className="overview-count">{on.length}</span>
              </h3>
              {groupByRoom(on).map(([room, items]) => (
                <div key={room} className="overview-room">
                  <div className="overview-room__label">{room}</div>
                  <div className="overview-tiles">
                    {items.map((item, i) => <DeviceTile key={`${room}-${i}`} item={item} />)}
                  </div>
                </div>
              ))}
            </section>
          )}
          {climate.length > 0 && (
            <section>
              <h3 className="overview-heading">Climate</h3>
              <div className="overview-tiles">
                {climate.map((item, i) => (
                  <DeviceTile key={`c-${i}`} item={{ ...item, domain: 'climate' }} />
                ))}
              </div>
            </section>
          )}
          {cameras.length > 0 && (
            <section>
              <h3 className="overview-heading">Cameras</h3>
              <div className="overview-cameras">
                {cameras.map((camera) => (
                  <CameraThumb key={camera.streamPath} client={client} camera={camera} />
                ))}
              </div>
            </section>
          )}
          {on.length === 0 && climate.length === 0 && cameras.length === 0 && (
            <p className="overview-empty">Nothing on right now.</p>
          )}
        </>
      )}
    </div>
  );
}

export function Surface({ client }: { client: BridgeClient | null }): JSX.Element | null {
  const surface = useNova((store) => store.surface);
  const dismissSurface = useNova((store) => store.dismissSurface);
  const dismissTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (!surface) return undefined;
    pulseCore(0.5);
    dismissTimer.current = setTimeout(dismissSurface, AUTO_DISMISS_MS);
    return () => {
      if (dismissTimer.current) clearTimeout(dismissTimer.current);
    };
  }, [surface, dismissSurface]);

  // Escape closes the surface first, before it falls through to whatever
  // App.tsx's own Escape handler does (voice-cancel).
  useEffect(() => {
    if (!surface) return undefined;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation();
        dismissSurface();
      }
    };
    window.addEventListener('keydown', onKey, { capture: true });
    return () => window.removeEventListener('keydown', onKey, { capture: true });
  }, [surface, dismissSurface]);

  if (!surface || !client) return null;

  return (
    <div className="surface" role="dialog" aria-label={surface.title}>
      <div className="surface__card">
        <header className="surface__bar">
          <h2 className="surface__title">{surface.title}</h2>
          <button
            type="button"
            className="surface__close"
            onClick={dismissSurface}
            aria-label="Close"
          >
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M6 6l12 12M18 6L6 18" />
            </svg>
          </button>
        </header>

        {renderSurface(surface, client)}
      </div>
    </div>
  );
}

function renderSurface(
  surface: NonNullable<ReturnType<typeof useNova.getState>['surface']>,
  client: BridgeClient,
): JSX.Element {
  switch (surface.kind) {
    case 'map':
      return <MapView lat={surface.lat} lon={surface.lon} />;
    case 'camera':
      return <CameraView client={client} streamPath={surface.streamPath} />;
    case 'agenda':
      return <AgendaView days={surface.days} />;
    case 'weather':
      return (
        <WeatherView
          place={surface.place}
          unit={surface.unit}
          current={surface.current}
          hours={surface.hours}
          days={surface.days}
        />
      );
    case 'home-overview':
      return (
        <HomeOverviewView
          client={client}
          on={surface.on}
          climate={surface.climate}
          open={surface.open}
          unlocked={surface.unlocked}
          cameras={surface.cameras}
        />
      );
  }
}
