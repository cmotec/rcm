import { useEffect, useState } from 'react';
import { csrfHeader, errorMessage, get } from '../api/client';
import type { SessionInfo } from '../api/types';
import { APP_NAME } from '../appName';
import { adoptSession, loadPreferences } from '../sessions/registry';
import { useWorkspace } from '../state/workspace';
import { syncAppFullscreen } from './appFullscreen';
import { SessionFrame } from './SessionFrame';

/** The path of a session opened in its own window: /popup/session/<id>. */
export function popupSessionId(pathname: string): string | null {
  const m = /^\/popup\/session\/([0-9a-fA-F-]{36})\/?$/.exec(pathname);
  return m ? m[1] : null;
}

/**
 * A single session in its own browser window. The session keeps running on the server: this window
 * attaches to it (the main window has let go of it). Closing the window ends the session, like the
 * window's close button.
 */
export function PopupSessionView({ sessionId }: { sessionId: string }) {
  const [error, setError] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const present = useWorkspace((s) => Boolean(s.sessions[sessionId]));

  useEffect(() => syncAppFullscreen(), []);
  useEffect(() => {
    void loadPreferences(); // e.g. "Mac: Command acts as Ctrl"
  }, []);

  // closing the browser window ends the session (keepalive: the request outlives the page)
  useEffect(() => {
    const end = (e: PageTransitionEvent) => {
      if (e.persisted || !ready) return;
      void fetch(`/api/v1/sessions/${sessionId}`, {
        method: 'DELETE', keepalive: true, credentials: 'same-origin', headers: csrfHeader(),
      }).catch(() => undefined);
    };
    window.addEventListener('pagehide', end);
    return () => window.removeEventListener('pagehide', end);
  }, [sessionId, ready]);

  useEffect(() => {
    let cancelled = false;
    get<SessionInfo>(`/api/v1/sessions/${sessionId}`)
      .then((info) => {
        if (cancelled) return;
        document.title = `${info.connection_name} – ${APP_NAME}`;
        adoptSession(info, () => window.close());
        setReady(true);
      })
      .catch((err) => !cancelled && setError(errorMessage(err)));
    return () => {
      cancelled = true;
    };
  }, [sessionId]);

  // disconnected with the window's close button: nothing left to show
  useEffect(() => {
    if (ready && !present) window.close();
  }, [ready, present]);

  if (error) return <div className="splash error">This session is no longer available: {error}</div>;
  if (!ready || !present) return <div className="splash">Opening the session…</div>;
  return (
    <div className="popup-session">
      <SessionFrame sessionId={sessionId} mode="tab" standalone />
    </div>
  );
}
