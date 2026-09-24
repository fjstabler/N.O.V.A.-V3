/**
 * Am I on a wall panel?
 *
 * One definition, used by both the layout and the renderer. That matters more
 * than it sounds: on a panel the clock sits *inside* the Core, and the Core has
 * to leave a hole for it. If CSS decided "panel" by one rule and the renderer
 * by another, the two would disagree at some screen size and the rings would
 * be drawn straight through the time — which is the single thing this layout
 * must never do.
 *
 * So the query lives here, JavaScript stamps `data-panel` on the root element,
 * and the stylesheet keys off that attribute rather than repeating the query.
 */

/**
 * An Echo Show 5 is 960×480, its bigger sibling 1280×800. The height bound is
 * what identifies them; `pointer: coarse` catches a touch panel that happens
 * to be wider, and the width bound catches a panel whose browser reports no
 * pointer at all — which the Echo Show's WebView does.
 */
export const PANEL_QUERY =
  '(max-height: 560px) and (pointer: coarse), (max-height: 560px) and (max-width: 1100px)';

/**
 * How much of the Core's middle is left empty for the clock, as a fraction of
 * the smaller screen dimension.
 *
 * 0.3 of 480 px is a clear circle 288 px across. Every ring is pushed outside
 * it — see `RING_CLEARANCE` in the tests, which checks the clock's box against
 * each ring's ellipse rather than trusting this number.
 */
export const PANEL_HOLLOW = 0.3;

/**
 * The Core is scaled slightly down on a panel, because pushing every ring out
 * by `PANEL_HOLLOW` makes the whole assembly larger and the outermost one would
 * otherwise run off the top and bottom of a 480 px screen.
 *
 * The result is still considerably bigger than before: outer diameter goes from
 * about 460 px to about 570 px, with the middle 290 px of it now empty.
 *
 * The exact figure is set by the tallest ring at the largest scale the panel
 * allows — `panel-layout.test.ts` is what holds it to a screen 480 px high.
 */
export const PANEL_CORE_SCALE = 0.61;

/**
 * The smallest tilt a ring is allowed on a panel.
 *
 * A hollow middle is not on its own enough to protect the clock. The rings are
 * ellipses, and a ring flattened to a tilt of 0.3 has a vertical radius a third
 * of its horizontal one — so it can sit well outside the hollow and still pass
 * straight through the band of screen the digits occupy. Raising the floor
 * keeps every ring rounder than the clock's own aspect, which is what actually
 * guarantees it passes around rather than across.
 */
export const PANEL_MIN_TILT = 0.55;

/**
 * How far the Core Scale setting may move the assembly on a panel.
 *
 * On a desktop the slider runs 0.5 to 1.6 and nothing depends on where it
 * lands. Here both ends break something: small pulls the rings in until they
 * cross the clock, large runs the outermost ring off the top and bottom of a
 * 480 px screen. The setting still does something, within a range where the
 * layout holds.
 */
export const PANEL_SCALE_RANGE = { min: 0.85, max: 1.15 };

/** The Core's scale on a panel, for a given Core Scale setting. */
export function panelCoreScale(setting: number): number {
  const clamped = Math.min(Math.max(setting, PANEL_SCALE_RANGE.min), PANEL_SCALE_RANGE.max);
  return clamped * PANEL_CORE_SCALE;
}

function media(): MediaQueryList | null {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return null;
  return window.matchMedia(PANEL_QUERY);
}

export function isPanel(): boolean {
  return media()?.matches ?? false;
}

/**
 * Call `onChange` whenever the answer changes, and stamp the root element so
 * the stylesheet can follow. Returns an unsubscribe.
 */
export function watchPanel(onChange: (panel: boolean) => void): () => void {
  const query = media();
  const apply = (panel: boolean) => {
    if (typeof document !== 'undefined') {
      if (panel) document.documentElement.dataset.panel = 'true';
      else delete document.documentElement.dataset.panel;
    }
    onChange(panel);
  };

  apply(query?.matches ?? false);
  if (!query) return () => undefined;

  const listener = (event: MediaQueryListEvent) => apply(event.matches);
  // `addListener` is the deprecated spelling, and the only one some older
  // WebViews have — which is exactly what a panel like this runs.
  if (typeof query.addEventListener === 'function') {
    query.addEventListener('change', listener);
    return () => query.removeEventListener('change', listener);
  }
  query.addListener(listener);
  return () => query.removeListener(listener);
}
