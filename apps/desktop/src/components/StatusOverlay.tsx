/**
 * Ambient status: connection health and the developer readout.
 *
 * Both are deliberately marginal. The connection notice only appears when the
 * core is unreachable — that is the one failure the user cannot diagnose from
 * the Core alone, since a disconnected UI would otherwise animate happily
 * forever while nothing behind it worked.
 */

import { useNova } from '@/state/store';

export function ConnectionStatus(): JSX.Element | null {
  const connection = useNova((store) => store.connection);
  if (connection === 'connected') return null;

  const message =
    connection === 'connecting'
      ? 'Starting core service'
      : connection === 'reconnecting'
        ? 'Reconnecting to core service'
        : 'Core service offline';

  return (
    <div className={`connection connection--${connection}`} role="status">
      <span className="connection__pip" />
      {message}
    </div>
  );
}

export function DeveloperReadout(): JSX.Element | null {
  const developer = useNova((store) => store.settings?.developer);
  const fps = useNova((store) => store.fps);
  const state = useNova((store) => store.state);
  const metrics = useNova((store) => store.metrics);

  const showFps = developer?.show_fps ?? false;
  const showState = developer?.show_state_badge ?? false;
  if (!showFps && !showState) return null;

  return (
    <div className="readout">
      {showState && <span className="readout__item">{state}</span>}
      {showFps && <span className="readout__item">{fps.toFixed(0)} fps</span>}
      {showFps && metrics && (
        <>
          <span className="readout__item">cpu {metrics.cpu.percent.toFixed(0)}%</span>
          <span className="readout__item">mem {metrics.memory.percent.toFixed(0)}%</span>
          {metrics.gpus[0] && (
            <span className="readout__item">
              gpu {metrics.gpus[0].utilisation.toFixed(0)}% {metrics.gpus[0].temperatureC.toFixed(0)}°
            </span>
          )}
        </>
      )}
    </div>
  );
}
