const DESIGN_WIDTH = 3840;

/** The design is authored at 3840px wide; zoom it to the viewport so a 1920 screen
 *  gets exactly "halve every px value" without touching any screen's CSS. */
export function zoomFor(viewportWidth: number): number {
  return Math.max(0.25, Math.min(1.5, viewportWidth / DESIGN_WIDTH));
}
