import { create } from 'zustand';
import type { Protocol } from '../api/types';

/** A connection being opened: the server has not created the session yet. */
export interface PendingConnect {
  key: string;
  name: string;
  protocol: Protocol;
  step: string;
  startedAt: number;
  /** Hidden while a dialog (credentials, host key) waits for the user. */
  waiting: boolean;
  cancelled: boolean;
}

interface ConnectingState {
  pending: PendingConnect[];
}

export const useConnecting = create<ConnectingState>(() => ({ pending: [] }));

function patch(key: string, change: Partial<PendingConnect>): void {
  useConnecting.setState((s) => ({ pending: s.pending.map((p) => (p.key === key ? { ...p, ...change } : p)) }));
}

export interface ConnectHandle {
  step(text: string): void;
  /** A dialog is asking the user something: hide the card meanwhile. */
  waiting(on: boolean): void;
  readonly cancelled: boolean;
  end(): void;
}

/** Show the loading card for a connection until `end()`. */
export function beginConnect(name: string, protocol: Protocol): ConnectHandle {
  const key = crypto.randomUUID();
  useConnecting.setState((s) => ({
    pending: [...s.pending, { key, name, protocol, step: 'Preparing the connection…', startedAt: Date.now(), waiting: false, cancelled: false }],
  }));
  return {
    step: (text) => patch(key, { step: text }),
    waiting: (on) => patch(key, { waiting: on }),
    get cancelled() {
      return useConnecting.getState().pending.find((p) => p.key === key)?.cancelled ?? true;
    },
    end: () => useConnecting.setState((s) => ({ pending: s.pending.filter((p) => p.key !== key) })),
  };
}

/** The user gave up waiting: the session is closed as soon as the server returns it. */
export function cancelConnect(key: string): void {
  patch(key, { cancelled: true });
}
