/**
 * The hidden settings panel.
 *
 * Every control is generated from the schema the core sends, so this file knows
 * nothing about individual settings — adding a field to the pydantic model in
 * `nova/config/schema.py` makes it appear here with the right control, bounds
 * and help text, and there is no second list to keep in sync.
 *
 * Edits are staged locally and sent as one patch on save, so a half-typed URL
 * never reaches the core. Secrets arrive redacted and are only sent back when
 * the user actually replaces them.
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { REDACTED, Requests, type SettingsField, type SettingsSection } from '@protocol';
import type { BridgeClient } from '@/lib/bridge';
import { useNova, type Settings } from '@/state/store';

type Draft = Record<string, unknown>;

export interface DeviceOption {
  value: string;
  label: string;
}

/** Live Home Assistant devices/rooms, so a routine's device field is a dropdown. */
const HomeDevicesContext = createContext<DeviceOption[]>([]);

/** Domains worth offering as a routine target. */
const CONTROLLABLE_DOMAINS = new Set([
  'light',
  'switch',
  'fan',
  'media_player',
  'cover',
  'lock',
  'scene',
  'script',
  'climate',
]);

interface HomeEntity {
  name: string;
  area?: string;
  domain: string;
}

/**
 * Turn the live Home Assistant entity list into dropdown options: whole rooms
 * first (so "the bedroom" is easy to pick), then the controllable devices,
 * de-duplicated by name and labelled with their room.
 */
export function buildDeviceOptions(entities: HomeEntity[]): DeviceOption[] {
  const areas = Array.from(
    new Set(entities.map((entity) => entity.area).filter((area): area is string => Boolean(area))),
  )
    .map((area) => ({ value: area, label: `${area} (whole room)` }))
    .sort((a, b) => a.label.localeCompare(b.label));

  const seen = new Set<string>();
  const devices = entities
    .filter((entity) => CONTROLLABLE_DOMAINS.has(entity.domain) && entity.name)
    .filter((entity) => (seen.has(entity.name) ? false : (seen.add(entity.name), true)))
    .map((entity) => ({
      value: entity.name,
      label: entity.area ? `${entity.name} — ${entity.area}` : entity.name,
    }))
    .sort((a, b) => a.label.localeCompare(b.label));

  return [...areas, ...devices];
}

/** Read a dotted path out of a nested object. */
export function read(source: unknown, path: string[]): unknown {
  return path.reduce<unknown>(
    (node, key) => (node && typeof node === 'object' ? (node as Draft)[key] : undefined),
    source,
  );
}

/**
 * Immutably write a dotted path, creating intermediate objects as needed.
 *
 * A path segment that indexes into an array (a list-of-groups item, e.g.
 * `accounts.0.name`) has to clone that array, not spread it as a plain
 * object — `{...anArray, "0": x}` drops the array itself and produces
 * `{0: x}`, which then reads back as `Array.isArray() === false` and the
 * whole list renders as empty. That is what "typing into a newly added
 * account makes it vanish" actually was: the first keystroke silently
 * turned the accounts array into a plain object.
 */
export function write(source: Draft, path: string[], value: unknown): Draft {
  const [head, ...rest] = path;
  if (!head) return source;
  if (rest.length === 0) return { ...source, [head]: value };
  const child = source[head];
  if (Array.isArray(child)) {
    return { ...source, [head]: writeIndex(child, rest, value) };
  }
  return { ...source, [head]: write((child ?? {}) as Draft, rest, value) };
}

function writeIndex(source: unknown[], path: string[], value: unknown): unknown[] {
  const [head, ...rest] = path;
  const index = Number(head);
  const next = [...source];
  if (rest.length === 0) {
    next[index] = value;
  } else {
    const child = next[index];
    next[index] = Array.isArray(child)
      ? writeIndex(child, rest, value)
      : write((child ?? {}) as Draft, rest, value);
  }
  return next;
}

interface FieldProps {
  field: SettingsField;
  path: string[];
  value: unknown;
  onChange: (path: string[], value: unknown) => void;
}

function Field({ field, path, value, onChange }: FieldProps): JSX.Element | null {
  const id = path.join('.');
  const set = (next: unknown) => onChange(path, next);
  const homeDevices = useContext(HomeDevicesContext);

  if (field.control === 'group') {
    return (
      <fieldset className="settings__group">
        <legend>{field.label}</legend>
        {field.fields?.map((child) => (
          <Field
            key={child.key}
            field={child}
            path={[...path, child.key]}
            value={read(value, [child.key])}
            onChange={onChange}
          />
        ))}
      </fieldset>
    );
  }

  if (field.control === 'list-of-groups') {
    const items = Array.isArray(value) ? (value as Draft[]) : [];
    const blank = Object.fromEntries(
      (field.fields ?? []).map((child) => [child.key, child.default ?? '']),
    );
    return (
      <fieldset className="settings__group">
        <legend>{field.label}</legend>
        {field.help && <p className="settings__help">{field.help}</p>}
        {items.map((item, index) => (
          <div className="settings__list-item" key={index}>
            <div className="settings__list-header">
              <span>
                {field.itemLabelKey ? String(item[field.itemLabelKey] ?? '') || `#${index + 1}` : `#${index + 1}`}
              </span>
              <button
                type="button"
                className="settings__remove"
                onClick={() => set(items.filter((_, i) => i !== index))}
              >
                Remove
              </button>
            </div>
            {field.fields?.map((child) => (
              <Field
                key={child.key}
                field={child}
                path={[...path, String(index), child.key]}
                value={item[child.key]}
                onChange={onChange}
              />
            ))}
          </div>
        ))}
        <button type="button" className="settings__add" onClick={() => set([...items, blank])}>
          Add
        </button>
      </fieldset>
    );
  }

  const row = (control: JSX.Element) => (
    <div className="settings__field">
      <label className="settings__label" htmlFor={id}>
        {field.label}
        {field.help && <span className="settings__help">{field.help}</span>}
      </label>
      {control}
    </div>
  );

  switch (field.control) {
    case 'toggle':
      return row(
        <button
          type="button"
          id={id}
          role="switch"
          aria-checked={value === true}
          className={`toggle${value === true ? ' toggle--on' : ''}`}
          onClick={() => set(!(value === true))}
        >
          <span className="toggle__thumb" />
        </button>,
      );

    case 'slider':
      return row(
        <div className="settings__slider">
          <input
            id={id}
            type="range"
            min={field.min ?? 0}
            max={field.max ?? 1}
            step={field.step ?? 0.05}
            value={Number(value ?? field.default ?? 0)}
            onChange={(event) => set(Number(event.target.value))}
          />
          <output>{Number(value ?? 0).toFixed(2)}</output>
        </div>,
      );

    case 'number':
      return row(
        <input
          id={id}
          type="number"
          className="settings__input"
          min={field.min}
          max={field.max}
          value={Number(value ?? field.default ?? 0)}
          onChange={(event) => set(Number(event.target.value))}
        />,
      );

    case 'select': {
      const fromHome = field.optionsSource === 'home_devices';
      const options = fromHome ? homeDevices : (field.options ?? []);
      // A device dropdown with nothing live to show (Home Assistant not
      // connected) falls back to a text box, so the routine can still be typed.
      if (fromHome && options.length === 0) {
        return row(
          <input
            id={id}
            type="text"
            className="settings__input"
            placeholder="device or room name"
            value={String(value ?? '')}
            onChange={(event) => set(event.target.value)}
            spellCheck={false}
          />,
        );
      }
      return row(
        <select
          id={id}
          className="settings__input"
          value={String(value ?? field.default ?? '')}
          onChange={(event) => set(event.target.value)}
        >
          {fromHome && <option value="">Choose a device or room…</option>}
          {options.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>,
      );
    }

    case 'password':
      return row(
        <input
          id={id}
          type="password"
          className="settings__input"
          placeholder={value === REDACTED ? 'stored — type to replace' : 'not set'}
          value={value === REDACTED ? '' : String(value ?? '')}
          onChange={(event) => set(event.target.value)}
          autoComplete="off"
          spellCheck={false}
        />,
      );

    case 'textarea':
      return row(
        <textarea
          id={id}
          className="settings__input settings__input--area"
          rows={5}
          value={String(value ?? '')}
          onChange={(event) => set(event.target.value)}
        />,
      );

    case 'string-list':
      return row(
        <input
          id={id}
          type="text"
          className="settings__input"
          value={Array.isArray(value) ? value.join(', ') : ''}
          placeholder="comma separated"
          onChange={(event) =>
            set(
              event.target.value
                .split(',')
                .map((entry) => entry.trim())
                .filter(Boolean),
            )
          }
        />,
      );

    default:
      return row(
        <input
          id={id}
          type="text"
          className="settings__input"
          value={String(value ?? '')}
          onChange={(event) => set(event.target.value)}
          spellCheck={false}
        />,
      );
  }
}

export function SettingsPanel({ client }: { client: BridgeClient | null }): JSX.Element | null {
  const open = useNova((store) => store.settingsOpen);
  const toggle = useNova((store) => store.toggleSettings);
  const settings = useNova((store) => store.settings);
  const sections = useNova((store) => store.settingsSchema);
  const services = useNova((store) => store.services);
  const degraded = useNova((store) => store.degraded);

  const [draft, setDraft] = useState<Draft>({});
  const [active, setActive] = useState<string>('assistant');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [deviceOptions, setDeviceOptions] = useState<DeviceOption[]>([]);

  // Pull the live Home Assistant devices so the routine editor's device fields
  // are dropdowns of what actually exists, not free text. Silent on failure —
  // those fields simply fall back to a text box (see the select control).
  useEffect(() => {
    if (!open || !client) return;
    let cancelled = false;
    void client
      .request<{ entities?: HomeEntity[] }>(Requests.HomeEntities, { domain: '' })
      .then((response) => {
        if (!cancelled) setDeviceOptions(buildDeviceOptions(response.entities ?? []));
      })
      .catch(() => {
        /* Home Assistant not connected — leave the fields as text inputs. */
      });
    return () => {
      cancelled = true;
    };
  }, [open, client]);

  // Re-seed the draft whenever the panel opens, so a cancelled edit is not
  // silently carried into the next session.
  useEffect(() => {
    if (open && settings) {
      setDraft(structuredClone(settings) as Draft);
      setError(null);
    }
  }, [open, settings]);

  const change = useCallback((path: string[], value: unknown) => {
    setDraft((current) => write(current, path, value));
  }, []);

  const dirty = useMemo(
    () => settings !== null && JSON.stringify(draft) !== JSON.stringify(settings),
    [draft, settings],
  );

  const save = useCallback(async () => {
    if (!client || !dirty) return;
    setSaving(true);
    setError(null);
    try {
      const response = await client.request<{ settings: Settings }>(Requests.SettingsSet, {
        patch: draft,
      });
      useNova.getState().setSettings(response.settings);
      setDraft(structuredClone(response.settings) as Draft);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setSaving(false);
    }
  }, [client, dirty, draft]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') toggle(false);
      if (event.key === 's' && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        void save();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, toggle, save]);

  if (!open) return null;

  const activeSection: SettingsSection | undefined =
    sections.find((section) => section.key === active) ?? sections[0];

  return (
    <>
      <div className="settings__scrim" onClick={() => toggle(false)} role="presentation" />
      <HomeDevicesContext.Provider value={deviceOptions}>
      <aside className="settings" role="dialog" aria-label="Settings">
        <header className="settings__header">
          <h2>Settings</h2>
          <button type="button" className="settings__close" onClick={() => toggle(false)}>
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M6 6l12 12M18 6L6 18" />
            </svg>
          </button>
        </header>

        <div className="settings__body">
          <nav className="settings__nav">
            {sections.map((section) => (
              <button
                key={section.key}
                type="button"
                className={`settings__nav-item${section.key === activeSection?.key ? ' is-active' : ''}`}
                onClick={() => setActive(section.key)}
              >
                {section.label}
              </button>
            ))}
            <div className="settings__nav-divider" />
            <button
              type="button"
              className={`settings__nav-item${active === '__status' ? ' is-active' : ''}`}
              onClick={() => setActive('__status')}
            >
              Status
            </button>
          </nav>

          <div className="settings__content">
            {active === '__status' ? (
              <section>
                <h3 className="settings__section-title">Subsystems</h3>
                <p className="settings__section-help">
                  What is running, and what is unavailable on this machine.
                </p>
                <ul className="status-list">
                  {services.map((service) => (
                    <li key={service.name} className={`status-list__item status--${service.state}`}>
                      <span className="status-list__dot" />
                      <span className="status-list__name">{service.name}</span>
                      <span className="status-list__state">{service.state}</span>
                      {service.detail && (
                        <span className="status-list__detail">{service.detail}</span>
                      )}
                    </li>
                  ))}
                </ul>
                {degraded.length > 0 && (
                  <>
                    <h3 className="settings__section-title">Unavailable capabilities</h3>
                    <ul className="status-list">
                      {degraded.map((notice) => (
                        <li key={notice.capability} className="status-list__item status--degraded">
                          <span className="status-list__dot" />
                          <span className="status-list__name">{notice.capability}</span>
                          <span className="status-list__detail">
                            {notice.message}
                            {notice.remedy && <code> {notice.remedy}</code>}
                          </span>
                        </li>
                      ))}
                    </ul>
                  </>
                )}
              </section>
            ) : (
              activeSection && (
                <section key={activeSection.key}>
                  <h3 className="settings__section-title">{activeSection.label}</h3>
                  <p className="settings__section-help">{activeSection.description}</p>
                  {activeSection.fields.map((field) => (
                    <Field
                      key={field.key}
                      field={field}
                      path={[activeSection.key, field.key]}
                      value={read(draft, [activeSection.key, field.key])}
                      onChange={change}
                    />
                  ))}
                </section>
              )
            )}
          </div>
        </div>

        <footer className="settings__footer">
          {error && <span className="settings__error">{error}</span>}
          <span className="settings__hint">{dirty ? 'Unsaved changes' : 'All changes saved'}</span>
          <button
            type="button"
            className="settings__save"
            disabled={!dirty || saving}
            onClick={() => void save()}
          >
            {saving ? 'Saving…' : 'Save'}
          </button>
        </footer>
      </aside>
      </HomeDevicesContext.Provider>
    </>
  );
}
