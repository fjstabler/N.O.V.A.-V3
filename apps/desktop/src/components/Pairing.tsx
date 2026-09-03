/**
 * First-run pairing, for the browser only.
 *
 * The Electron shell is handed a token over IPC and never sees this. A page —
 * a phone, a wall panel — has to be told once which core it belongs to, and
 * the honest way to say "I cannot connect because you have not told me who you
 * are" is to ask, rather than to sit in a reconnect loop that looks exactly
 * like the core being switched off.
 *
 * The token is on the machine running the core, in
 * `~/.local/share/nova/bridge.json`. Opening the link the core prints fills
 * this in automatically; typing it is the fallback when a link cannot be
 * clicked, which on a device with no keyboard-sharing is most of the time.
 */

import { useState } from 'react';
import { storeToken } from '@/lib/session';

export function Pairing({ onPaired }: { onPaired: () => void }): JSX.Element {
  const [token, setToken] = useState('');

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    const value = token.trim();
    if (!value) return;
    storeToken(value);
    onPaired();
  };

  return (
    <div className="pairing">
      <form className="pairing__card" onSubmit={submit}>
        <h1 className="pairing__title">N.O.V.A.</h1>
        <p className="pairing__body">
          Enter the bridge token to pair this screen. It is on the machine running the core, in{' '}
          <code>~/.local/share/nova/bridge.json</code>.
        </p>
        <input
          className="pairing__input"
          value={token}
          onChange={(event) => setToken(event.target.value)}
          placeholder="bridge token"
          spellCheck={false}
          autoComplete="off"
          autoCapitalize="off"
          autoCorrect="off"
          aria-label="Bridge token"
        />
        <button className="pairing__submit" type="submit" disabled={!token.trim()}>
          Pair
        </button>
      </form>
    </div>
  );
}
