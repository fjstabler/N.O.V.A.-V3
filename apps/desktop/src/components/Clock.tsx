/**
 * The clock.
 *
 * One of only two things on screen when idle, so its restraint matters. Tabular
 * figures keep the digits from shifting as they change — a proportional font
 * makes a clock visibly twitch every second, which reads as cheap.
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

export function Clock(): JSX.Element {
  const appearance = useNova((store) => store.settings?.appearance);
  const state = useNova((store) => store.state);

  const twentyFourHour = appearance?.clock_24_hour ?? true;
  const showSeconds = appearance?.show_seconds ?? false;
  const showDate = appearance?.show_date ?? true;
  const now = useTick(showSeconds);

  const time = new Intl.DateTimeFormat(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    ...(showSeconds ? { second: '2-digit' } : {}),
    hour12: !twentyFourHour,
  }).format(now);

  const date = new Intl.DateTimeFormat(undefined, {
    weekday: 'long',
    day: 'numeric',
    month: 'long',
  }).format(now);

  // The clock recedes while N.O.V.A. is working so the Core holds attention.
  const busy = state !== 'idle' && state !== 'booting';

  return (
    <div className={`clock${busy ? ' clock--receded' : ''}`}>
      <div className="clock__time">{time}</div>
      {showDate && <div className="clock__date">{date}</div>}
    </div>
  );
}
