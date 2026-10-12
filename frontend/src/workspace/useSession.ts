import { useEffect, useState } from 'react';
import type { SessionSnapshot } from '../sessions/base';
import { getController } from '../sessions/registry';

const EMPTY: SessionSnapshot = { status: 'connecting', message: null, detail: null };

/** Subscribe to a session controller's status. */
export function useSessionState(sessionId: string): SessionSnapshot {
  const [state, setState] = useState<SessionSnapshot>(() => getController(sessionId)?.state ?? EMPTY);
  useEffect(() => {
    const ctrl = getController(sessionId);
    if (!ctrl) return undefined;
    return ctrl.subscribe(setState);
  }, [sessionId]);
  return state;
}

export const SESSION_MIME = 'application/x-session-id';
/** A connection dragged from the tree (opens a new session where it is dropped). */
export const CONNECTION_MIME = 'application/x-connection';
