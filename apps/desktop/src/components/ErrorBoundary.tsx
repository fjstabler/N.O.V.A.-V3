/**
 * Somewhere to land when a render throws.
 *
 * React unmounts the entire tree on an uncaught render error, which leaves the
 * page background and nothing else. On a wall panel that is a black screen with
 * no console, no devtools and no way to read the exception — a `structuredClone`
 * missing from an older WebView presented identically to a dead GPU, a broken
 * stylesheet and a crashed renderer, and cost several rounds of guessing to
 * tell apart.
 *
 * So the point of this is not to recover. It is to put the error where someone
 * standing in front of the device can read it out.
 */

import { Component, type ErrorInfo, type ReactNode } from 'react';

interface Props {
  children: ReactNode;
  /** Names the part that failed, so the message says more than "it broke". */
  label?: string;
  /** Rendered instead of the default card — used where a panel can fail alone. */
  onReset?: () => void;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  override state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    // Also goes to the Android log, where `adb logcat` can find it if anyone
    // has a cable and the inclination.
    console.error('[nova] render failed', error, info.componentStack);
  }

  override render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;

    return (
      <div className="crash" role="alert">
        <div className="crash__card">
          <h2 className="crash__title">{this.props.label ?? 'Something broke'}</h2>
          <p className="crash__message">{error.message}</p>
          {error.stack ? <pre className="crash__stack">{error.stack.slice(0, 900)}</pre> : null}
          {this.props.onReset ? (
            <button
              type="button"
              className="crash__button"
              onClick={() => {
                this.setState({ error: null });
                this.props.onReset?.();
              }}
            >
              Dismiss
            </button>
          ) : (
            <button
              type="button"
              className="crash__button"
              onClick={() => window.location.reload()}
            >
              Reload
            </button>
          )}
        </div>
      </div>
    );
  }
}
