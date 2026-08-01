/**
 * Transient conversation display.
 *
 * The spec forbids a chat log, and this is not one: it shows the current
 * utterance and the current reply, then clears. Nothing accumulates, nothing
 * scrolls, and there is no history to scroll back through.
 *
 * It exists because a purely audio response leaves the user guessing when
 * recognition mishears them — seeing the transcript for a moment is the
 * difference between "it's broken" and "it misheard me".
 */

import { useNova } from '@/state/store';

function toolLabel(tool: string): string {
  // `server_restart_container` → `Server · restart container`
  const parts = tool.split('_');
  const skill = parts[0] ?? tool;
  const action = parts.slice(1).join(' ');
  if (!action) return skill;
  return `${skill.charAt(0).toUpperCase()}${skill.slice(1)} · ${action}`;
}

export function Conversation(): JSX.Element | null {
  const transcript = useNova((store) => store.transcript);
  const reply = useNova((store) => store.reply);
  const activeTool = useNova((store) => store.activeTool);
  const state = useNova((store) => store.state);

  if (!transcript && !reply && !activeTool) return null;

  return (
    <div className="conversation" role="status" aria-live="polite">
      {transcript && (
        <p key={transcript} className="conversation__utterance">
          {transcript}
        </p>
      )}
      {activeTool && !reply && (
        <p className="conversation__tool">
          <span className="conversation__tool-dot" />
          {toolLabel(activeTool)}
        </p>
      )}
      {reply && (
        <p className={`conversation__reply${state === 'speaking' ? ' is-speaking' : ''}`}>
          {reply}
        </p>
      )}
    </div>
  );
}
