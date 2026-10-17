import { create } from 'zustand';
import type { HostKeyInfo, PersonalAccountKind, Protocol } from '../api/types';

export interface MenuItem {
  label: string;
  action?: () => void;
  danger?: boolean;
  disabled?: boolean;
  separator?: boolean;
  icon?: React.ReactNode;
}

export interface InteractiveCredentials {
  username?: string;
  password?: string;
  domain?: string;
}

export type DialogRequest =
  | { id: number; kind: 'confirm'; title: string; message: string; confirmLabel?: string; danger?: boolean;
      resolve: (ok: boolean) => void }
  | { id: number; kind: 'prompt'; title: string; label: string; initial?: string; secret?: boolean;
      resolve: (value: string | null) => void }
  | { id: number; kind: 'credentials'; connectionName: string; protocol: Protocol; fields: string[];
      username?: string | null; domain?: string | null;
      resolve: (v: InteractiveCredentials | AccountOverride | null) => void }
  | { id: number; kind: 'hostkey'; info: HostKeyInfo; changed: boolean; resolve: (accept: boolean) => void }
  | { id: number; kind: 'choose-credential'; connectionId: string; connectionName: string; protocol: Protocol;
      folderId?: string | null;
      resolve: (o: AuthOverride | null) => void }
  | { id: number; kind: 'open-with'; connectionId: string; connectionName: string; protocol: Protocol;
      resolve: (o: AuthOverride | null) => void };

/** One of the user's own accounts (Profile → My accounts). */
export type AccountOverride = { mode: 'account'; account: PersonalAccountKind };

/** "Open with different credentials" (one session only). */
export type AuthOverride =
  | { mode: 'own'; credentials: InteractiveCredentials }
  | { mode: 'credential'; credential_id: string }
  | AccountOverride;

export interface Toast {
  id: number;
  kind: 'info' | 'success' | 'error' | 'warning';
  message: string;
}

interface DialogState {
  queue: DialogRequest[];
  menu: { x: number; y: number; items: MenuItem[] } | null;
  toasts: Toast[];
}

export const useDialogStore = create<DialogState>(() => ({ queue: [], menu: null, toasts: [] }));

let seq = 1;

type DistributiveOmit<T, K extends keyof T> = T extends unknown ? Omit<T, K> : never;

function enqueue<T>(req: DistributiveOmit<DialogRequest, 'id' | 'resolve'>): Promise<T> {
  return new Promise<T>((resolve) => {
    const id = seq++;
    const full = {
      ...req,
      id,
      resolve: (v: T) => {
        useDialogStore.setState((s) => ({ queue: s.queue.filter((q) => q.id !== id) }));
        resolve(v);
      },
    } as unknown as DialogRequest;
    useDialogStore.setState((s) => ({ queue: [...s.queue, full] }));
  });
}

export const dialogs = {
  confirm: (o: { title: string; message: string; confirmLabel?: string; danger?: boolean }) =>
    enqueue<boolean>({ kind: 'confirm', ...o }),
  prompt: (o: { title: string; label: string; initial?: string; secret?: boolean }) =>
    enqueue<string | null>({ kind: 'prompt', ...o }),
  credentials: (o: { connectionName: string; protocol: Protocol; fields: string[]; username?: string | null;
    domain?: string | null }) => enqueue<InteractiveCredentials | AccountOverride | null>({ kind: 'credentials', ...o }),
  hostKey: (info: HostKeyInfo, changed: boolean) => enqueue<boolean>({ kind: 'hostkey', info, changed }),
  /** "Choose at connect time": an entry from the credential store, one of the user's accounts, or typed credentials. */
  chooseCredential: (connectionId: string, connectionName: string, protocol: Protocol, folderId?: string | null) =>
    enqueue<AuthOverride | null>({ kind: 'choose-credential', connectionId, connectionName, protocol, folderId }),
  openWith: (connectionId: string, connectionName: string, protocol: Protocol) =>
    enqueue<AuthOverride | null>({ kind: 'open-with', connectionId, connectionName, protocol }),
  contextMenu: (x: number, y: number, items: MenuItem[]) => useDialogStore.setState({ menu: { x, y, items } }),
  closeMenu: () => useDialogStore.setState({ menu: null }),
  toast: (message: string, kind: Toast['kind'] = 'info') => {
    const id = seq++;
    useDialogStore.setState((s) => ({ toasts: [...s.toasts.slice(-4), { id, kind, message }] }));
    window.setTimeout(() => useDialogStore.setState((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
      kind === 'error' ? 9000 : 4000);
  },
};

/** Stable accessor so components can call dialog helpers like hooks. */
export const useDialogs = () => dialogs;
