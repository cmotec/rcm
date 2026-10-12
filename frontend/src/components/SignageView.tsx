import { useQuery } from '@tanstack/react-query';
import { LoaderCircle } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { get } from '../api/client';
import { keys } from '../api/hooks';
import type { Connection, Me } from '../api/types';
import type { SessionSnapshot } from '../sessions/base';
import { closeSessionById, getController, openConnection } from '../sessions/registry';

/** Waits between attempts to start the session again (the last one repeats). */
const RETRY_DELAYS_MS = [5_000, 10_000, 30_000, 60_000];
/** How often the display checks whether its assignment changed. */
const ASSIGNMENT_POLL_MS = 60_000;

/**
 * Signage account: one RDP/VNC connection, fullscreen, no menus. Starts after login, starts a new
 * session whenever the current one ends or fails, and follows assignment changes by the admin.
 */
export function SignageView() {
  const me = useQuery({
    queryKey: keys.me,
    queryFn: () => get<Me>('/api/v1/auth/me'),
    retry: false,
    refetchInterval: ASSIGNMENT_POLL_MS,
  });
  const connectionId = me.data?.signage_connection_id ?? null;
  const slot = useRef<HTMLDivElement>(null);
  const [notice, setNotice] = useState<{ busy: boolean; text: string } | null>({ busy: true, text: 'Connecting…' });

  useEffect(() => {
    if (!connectionId) {
      setNotice({ busy: false, text: 'No connection is assigned to this display.' });
      return undefined;
    }
    let disposed = false;
    let sessionId: string | null = null;
    let unsubscribe: (() => void) | null = null;
    let timer: number | null = null;
    let failures = 0;

    const stopSession = async () => {
      unsubscribe?.();
      unsubscribe = null;
      const id = sessionId;
      sessionId = null;
      if (id) await closeSessionById(id).catch(() => undefined);
    };

    const retryLater = (reason: string) => {
      if (disposed || timer !== null) return;
      const delay = RETRY_DELAYS_MS[Math.min(failures, RETRY_DELAYS_MS.length - 1)];
      failures += 1;
      setNotice({ busy: false, text: `${reason} Retrying in ${Math.round(delay / 1000)} s…` });
      timer = window.setTimeout(() => {
        timer = null;
        void start();
      }, delay);
    };

    const onStatus = (s: SessionSnapshot) => {
      if (disposed) return;
      if (s.status === 'connected') {
        failures = 0;
        setNotice(null);
      } else if (s.status === 'connecting' || s.status === 'reconnecting') {
        setNotice({ busy: true, text: s.message ?? 'Connecting…' });
      } else {
        // disconnected (own reconnects used up), error, or ended remotely: start a new session
        const reason = s.message ? `${s.message}.` : 'The connection ended.';
        void stopSession().then(() => retryLater(reason));
      }
    };

    const start = async () => {
      if (disposed) return;
      setNotice({ busy: true, text: 'Connecting…' });
      try {
        const conn = await get<Connection>(`/api/v1/connections/${connectionId}`);
        const id = await openConnection({ id: conn.id, name: conn.name, protocol: conn.protocol });
        const ctrl = id ? getController(id) : undefined;
        if (!id || !ctrl) {
          retryLater('Could not start the connection.');
          return;
        }
        if (disposed) {
          await closeSessionById(id);
          return;
        }
        sessionId = id;
        ctrl.onEnded = null; // ended sessions are restarted via the status listener, not closed for good
        slot.current?.appendChild(ctrl.element);
        ctrl.focus();
        unsubscribe = ctrl.subscribe(onStatus);
      } catch (err) {
        retryLater(err instanceof Error ? `${err.message}.` : 'Could not start the connection.');
      }
    };

    void start();
    return () => {
      disposed = true;
      if (timer !== null) window.clearTimeout(timer);
      void stopSession();
    };
  }, [connectionId]);

  // A user gesture is needed for real fullscreen; the first touch or click on the display asks for it.
  useEffect(() => {
    const goFullscreen = () => {
      if (!document.fullscreenElement) void document.documentElement.requestFullscreen?.().catch(() => undefined);
    };
    window.addEventListener('pointerdown', goFullscreen, { once: true, capture: true });
    return () => window.removeEventListener('pointerdown', goFullscreen, { capture: true });
  }, []);

  return (
    <div className="signage">
      <div ref={slot} className="signage-slot" />
      {notice && (
        <div className="signage-notice" role="status">
          {notice.busy && <LoaderCircle className="spin" size={22} />}
          <span>{notice.text}</span>
        </div>
      )}
    </div>
  );
}
