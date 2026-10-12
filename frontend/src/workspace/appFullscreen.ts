import { useWorkspace } from '../state/workspace';

type KeyboardLock = { lock?: () => Promise<void>; unlock?: () => void };
const keyboardApi = () => (navigator as unknown as { keyboard?: KeyboardLock }).keyboard;

/**
 * Show a session over the whole app and, where allowed, the whole screen. In browser fullscreen the
 * keyboard is locked (Chromium), so Esc, Alt+Tab-like shortcuts and the Windows key reach the remote;
 * holding Esc leaves fullscreen.
 */
export function enterAppFullscreen(sessionId: string): void {
  useWorkspace.getState().setFullscreen(sessionId);
  if (!document.fullscreenElement && document.documentElement.requestFullscreen) {
    document.documentElement.requestFullscreen()
      .then(() => keyboardApi()?.lock?.().catch(() => undefined))
      .catch(() => undefined); // not allowed (e.g. iOS): the app-wide view still applies
  }
}

export function exitAppFullscreen(): void {
  useWorkspace.getState().setFullscreen(null);
  keyboardApi()?.unlock?.();
  if (document.fullscreenElement) void document.exitFullscreen().catch(() => undefined);
}

/** Leaving browser fullscreen (Esc held, F11, …) also leaves the app-wide view. */
export function syncAppFullscreen(): () => void {
  const onChange = () => {
    if (!document.fullscreenElement && useWorkspace.getState().fullscreenSessionId) {
      useWorkspace.getState().setFullscreen(null);
    }
  };
  document.addEventListener('fullscreenchange', onChange);
  return () => document.removeEventListener('fullscreenchange', onChange);
}
