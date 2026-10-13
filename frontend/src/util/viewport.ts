/**
 * Phones/tablets: keep the focused content visible above the on-screen keyboard.
 *
 * The keyboard shrinks the visual viewport (the layout keeps its size). Two ways to react:
 * - remote desktops (RDP/VNC) keep their size and the app slides up by the keyboard height, so the
 *   bottom of the remote screen is visible without asking the remote side to resize (`--kb-shift`);
 * - everything else (terminals, forms) is sized to the visible area (`--app-height`).
 */
export function trackVisualViewport(): () => void {
  const vv = window.visualViewport;
  if (!vv) return () => undefined;
  const root = document.documentElement;
  let frame = 0;
  const apply = () => {
    frame = 0;
    if (Math.abs(vv.scale - 1) > 0.01) return; // the page is pinch-zoomed: that is not the keyboard
    const keyboard = Math.max(0, Math.round(window.innerHeight - vv.height));
    const remoteDesktop = Boolean(document.activeElement?.closest('.session-guac'));
    if (keyboard > 80 && remoteDesktop) {
      root.style.removeProperty('--app-height');
      root.style.setProperty('--kb-shift', `${keyboard}px`);
      root.classList.add('kb-shift');
    } else {
      root.classList.remove('kb-shift');
      root.style.removeProperty('--kb-shift');
      root.style.setProperty('--app-height', `${Math.round(vv.height)}px`);
    }
    // iOS pans the page to show the focused field; the app positions itself, so undo the pan
    if (window.scrollY || window.scrollX) window.scrollTo(0, 0);
  };
  const schedule = () => {
    if (!frame) frame = window.requestAnimationFrame(apply);
  };
  vv.addEventListener('resize', schedule);
  vv.addEventListener('scroll', schedule);
  document.addEventListener('focusin', schedule);
  document.addEventListener('focusout', schedule);
  apply();
  return () => {
    vv.removeEventListener('resize', schedule);
    vv.removeEventListener('scroll', schedule);
    document.removeEventListener('focusin', schedule);
    document.removeEventListener('focusout', schedule);
    if (frame) window.cancelAnimationFrame(frame);
    root.style.removeProperty('--app-height');
    root.style.removeProperty('--kb-shift');
    root.classList.remove('kb-shift');
  };
}
