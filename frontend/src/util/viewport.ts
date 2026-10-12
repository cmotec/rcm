/**
 * Phones/tablets: keep the app inside the visible area. When the on-screen keyboard opens, the
 * visual viewport shrinks (iOS does not resize the layout and scrolls the page instead), so the app is
 * sized to the visual viewport and the remote desktop shrinks above the keyboard instead of under it.
 */
export function trackVisualViewport(): () => void {
  const vv = window.visualViewport;
  if (!vv) return () => undefined;
  const root = document.documentElement;
  let frame = 0;
  const apply = () => {
    frame = 0;
    if (Math.abs(vv.scale - 1) > 0.01) return; // the page is pinch-zoomed: that is not the keyboard
    root.style.setProperty('--app-height', `${Math.round(vv.height)}px`);
    // iOS pans the page to show the focused field; the app already fits, so undo the pan
    if (window.scrollY || window.scrollX) window.scrollTo(0, 0);
  };
  const schedule = () => {
    if (!frame) frame = window.requestAnimationFrame(apply);
  };
  vv.addEventListener('resize', schedule);
  vv.addEventListener('scroll', schedule);
  apply();
  return () => {
    vv.removeEventListener('resize', schedule);
    vv.removeEventListener('scroll', schedule);
    if (frame) window.cancelAnimationFrame(frame);
    root.style.removeProperty('--app-height');
  };
}
