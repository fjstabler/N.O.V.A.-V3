/**
 * The settings-draft path helpers.
 *
 * Regression: adding a calendar account and typing into its Name field made
 * the whole account vanish. Root cause was `write()` spreading an array as a
 * plain object once the path indexed into it (`{...anArray, "0": x}` drops
 * the array and yields `{0: x}`), so the list-of-groups renderer's
 * `Array.isArray()` check failed on the very next render.
 */

import { describe, expect, it } from 'vitest';
import { read, write } from './SettingsPanel';

describe('write', () => {
  it('replaces a whole list in one step, as Add/Remove do', () => {
    const draft = { calendar: { accounts: [] as unknown[] } };
    const next = write(draft, ['calendar', 'accounts'], [{ name: '', url: '' }]);
    expect(next.calendar).toMatchObject({ accounts: [{ name: '', url: '' }] });
  });

  it('keeps the accounts field an array after writing into one item', () => {
    const draft = { calendar: { accounts: [{ name: '', url: '', username: '' }] } };
    const next = write(draft, ['calendar', 'accounts', '0', 'name'], 'Apple');

    const accounts = (next.calendar as { accounts: unknown }).accounts;
    expect(Array.isArray(accounts)).toBe(true);
    expect(accounts).toEqual([{ name: 'Apple', url: '', username: '' }]);
  });

  it('survives several keystrokes in a row, like typing a whole name', () => {
    let draft: Record<string, unknown> = { calendar: { accounts: [{ name: '' }] } };
    for (const partial of ['A', 'Ap', 'App', 'Appl', 'Apple']) {
      draft = write(draft, ['calendar', 'accounts', '0', 'name'], partial);
    }
    expect(read(draft, ['calendar', 'accounts', '0', 'name'])).toBe('Apple');
    expect(Array.isArray(read(draft, ['calendar', 'accounts']))).toBe(true);
  });

  it('does not disturb other items in the same list', () => {
    const draft = {
      calendar: {
        accounts: [
          { name: 'Work', url: 'a' },
          { name: 'Personal', url: 'b' },
        ],
      },
    };
    const next = write(draft, ['calendar', 'accounts', '1', 'url'], 'https://caldav.icloud.com');

    const accounts = (next.calendar as { accounts: { name: string; url: string }[] }).accounts;
    expect(accounts[0]).toEqual({ name: 'Work', url: 'a' });
    expect(accounts[1]).toEqual({ name: 'Personal', url: 'https://caldav.icloud.com' });
  });

  it('does not mutate the original draft (immutability)', () => {
    const original = { calendar: { accounts: [{ name: '' }] } };
    const snapshot = JSON.parse(JSON.stringify(original));

    write(original, ['calendar', 'accounts', '0', 'name'], 'Apple');

    expect(original).toEqual(snapshot);
  });
});
