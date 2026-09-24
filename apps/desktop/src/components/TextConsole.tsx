/**
 * Hidden text console (Ctrl+Shift+K).
 *
 * Voice is the interface; this is a diagnostic. It exists so the assistant can
 * be exercised on a machine with no microphone, and so a failing turn can be
 * reproduced without shouting at it. It is never visible unless summoned, and
 * it is disabled entirely when `developer.text_console` is off.
 */

import { useEffect, useRef, useState } from 'react';
import { Requests } from '@protocol';
import type { BridgeClient } from '@/lib/bridge';
import { useNova } from '@/state/store';

export function TextConsole({ client }: { client: BridgeClient | null }): JSX.Element | null {
  const open = useNova((store) => store.consoleOpen);
  const toggle = useNova((store) => store.toggleConsole);
  const enabled = useNova((store) => store.settings?.developer?.text_console ?? true);
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') toggle(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, toggle]);

  if (!open || !enabled) return null;

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    const value = text.trim();
    if (!value || !client || busy) return;
    setBusy(true);
    setText('');
    try {
      await client.request(Requests.TextSubmit, { text: value });
    } catch (error) {
      console.error('[nova] text submission failed', error);
    } finally {
      setBusy(false);
      inputRef.current?.focus();
    }
  };

  return (
    <form className="console" onSubmit={submit}>
      <span className="console__prompt">›</span>
      <input
        ref={inputRef}
        className="console__input"
        value={text}
        onChange={(event) => setText(event.target.value)}
        placeholder={busy ? 'thinking…' : 'type a request'}
        disabled={busy}
        spellCheck={false}
        autoComplete="off"
      />
      <kbd className="console__hint">esc</kbd>
    </form>
  );
}
