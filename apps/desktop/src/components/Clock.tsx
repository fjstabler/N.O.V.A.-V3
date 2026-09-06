/**
 * The clock.
 *
 * Deliberately marginal: it sits in the top-left corner as a telemetry readout
 * rather than centre stage, so the Core is unambiguously the subject. The
 * styling follows the same language as the rest of the interface — a thin
 * accent rule, tabular figures, letterspaced small caps — so it reads as part
 * of N.O.V.A. rather than as a lock screen someone dropped on top of it.
 *
 * Seconds are always shown but set smaller and dimmer than hours and minutes.
 * They give the panel a visible pulse, which is what stops a corner readout
 * looking like a static image.
 *
 * The tick aligns to the wall clock rather than running on a fixed interval, so
 * the display never drifts a second behind after the machine sleeps.
 */

import { useEffect, useState } from 'react';
import { useNova } from '@/state/store';

function useTick(showSeconds: boolean): Date {
  const [now, setNow] = useState(() => new Date());

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout>;

    const schedule = () => {
      const current = new Date();
      setNow(current);
      const period = showSeconds ? 1000 : 60_000;
      // Fire just after the next boundary, not `period` from now.
      const delay = period - (current.getTime() % period) + 20;
      timer = setTimeout(schedule, delay);
    };

    schedule();
    return () => clearTimeout(timer);
  }, [showSeconds]);

  return now;
}

function pad(value: number): string {
  return value.toString().padStart(2, '0');
}

export function Clock(): JSX.Element {
  const appearance = useNova((store) => store.settings?.appearance);
  const state = useNova((store) => store.state);
  const connection = useNova((store) => store.connection);

  const twentyFourHour = appearance?.clock_24_hour ?? true;
  const showSeconds = appearance?.show_seconds ?? true;
  const showDate = appearance?.show_date ?? true;
  const now = useTick(showSeconds);

  const hours = twentyFourHour ? now.getHours() : now.getHours() % 12 || 12;
  const meridiem = twentyFourHour ? '' : now.getHours() < 12 ? 'AM' : 'PM';

  const date = new Intl.DateTimeFormat(undefined, {
    weekday: 'short',
    day: '2-digit',
    month: 'short',
  })
    .format(now)
    .toUpperCase();

  // While N.O.V.A. is working, the readout reports what it is doing instead of
  // the date — the corner becomes a status line rather than competing for
  // attention with the Core.
  const busy = state !== 'idle' && state !== 'booting';
  const status = connection !== 'connected' ? connection : busy ? state : '';

  return (
    <div className={`clock${busy ? ' clock--active' : ''}`}>
      <div className="clock__rule" />
      <div className="clock__readout">
        <div className="clock__time">
          <span className="clock__hm">
            {pad(hours)}
            <span className="clock__colon">:</span>
            {pad(now.getMinutes())}
          </span>
          {showSeconds && <span className="clock__seconds">{pad(now.getSeconds())}</span>}
          {meridiem && <span className="clock__meridiem">{meridiem}</span>}
        </div>
        {showDate && (
          <div className="clock__meta">
            <span className="clock__date">{date}</span>
            {status && (
              <>
                <span className="clock__separator">/</span>
                <span className="clock__status">{status}</span>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
