import { ApiError, errorMessage, get, post, put } from '../api/client';
import type { HostKeyInfo, Protocol, SessionInfo } from '../api/types';
import { dialogs, type InteractiveCredentials } from '../state/dialogs';
import { useWorkspace } from '../state/workspace';
import {
  type Edge,
  type Rect,
  type SavedLayout,
  closeSession as closeInLayout,
  fromSaved,
  savedConnectionIds,
  splitWith,
  toSaved,
} from '../workspace/model';
import type { BaseSession } from './base';
import { FileSession } from './FileSession';
import { GuacSession } from './GuacSession';
import { TerminalSession } from './TerminalSession';

/** Live session controllers, keyed by session id (kept outside React on purpose). */
const controllers = new Map<string, BaseSession>();

export function getController(id: string): BaseSession | undefined {
  return controllers.get(id);
}

function createController(info: SessionInfo): BaseSession {
  if (info.kind === 'guac') return new GuacSession(info);
  if (info.kind === 'terminal') return new TerminalSession(info);
  return new FileSession(info);
}

export interface OpenTarget {
  floating?: Rect;
  splitWith?: { sessionId: string; edge: Edge };
}

interface ConnectionRef {
  id: string;
  name: string;
  protocol: Protocol;
}

/**
 * Start a remote session for a connection: handles interactive credential prompts and SSH host-key
 * confirmation, then places the session in the workspace. Returns the session id, or null if cancelled.
 */
export async function openConnection(conn: ConnectionRef, target: OpenTarget = {}): Promise<string | null> {
  let credentials: InteractiveCredentials | undefined;
  let credentialId: string | undefined;
  const accepted: { hostname: string; port: number; fingerprint: string }[] = [];
  let info: SessionInfo | null = null;
  for (let attempt = 0; attempt < 8 && !info; attempt++) {
    try {
      info = await post<SessionInfo>('/api/v1/sessions', {
        connection_id: conn.id,
        credentials,
        credential_id: credentialId,
        accept_host_keys: accepted,
      });
    } catch (err) {
      if (err instanceof ApiError && err.code === 'credential_choice_required') {
        const chosen = await dialogs.chooseCredential(conn.name);
        if (!chosen) return null;
        credentialId = chosen;
        continue;
      }
      if (err instanceof ApiError && err.code === 'credentials_required') {
        const p = err.problem as unknown as { fields: string[]; username?: string; domain?: string };
        const entered = await dialogs.credentials({
          connectionName: conn.name,
          protocol: conn.protocol,
          fields: p.fields,
          username: credentials?.username ?? p.username,
          domain: credentials?.domain ?? p.domain,
        });
        if (!entered) return null;
        credentials = entered;
        continue;
      }
      if (err instanceof ApiError && (err.code === 'hostkey_unknown' || err.code === 'hostkey_changed')) {
        const hk = err.problem.host_key as HostKeyInfo | undefined;
        if (!hk) {
          dialogs.toast(err.message, 'error');
          return null;
        }
        const changed = err.code === 'hostkey_changed';
        if (changed && !hk.can_replace) {
          await dialogs.hostKey(hk, true);
          return null;
        }
        const ok = await dialogs.hostKey(hk, changed);
        if (!ok) return null;
        accepted.push({ hostname: hk.hostname, port: hk.port, fingerprint: hk.fingerprint });
        continue;
      }
      dialogs.toast(`${conn.name}: ${errorMessage(err)}`, 'error');
      return null;
    }
  }
  if (!info) return null;
  const ctrl = createController(info);
  controllers.set(info.id, ctrl);
  useWorkspace.getState().addSession(
    {
      id: info.id,
      connectionId: conn.id,
      name: conn.name,
      protocol: info.protocol,
      kind: info.kind,
      host: info.effective_host,
      warnings: info.warnings,
    },
    { floating: target.floating },
  );
  if (target.splitWith) {
    const { sessionId, edge } = target.splitWith;
    useWorkspace.getState().update((l) => splitWith(l, sessionId, info!.id, edge));
  }
  if (info.host_resolution_note) dialogs.toast(`${conn.name}: ${info.host_resolution_note}`, 'warning');
  return info.id;
}

/** Close a session (server side too) and remove it from the layout. */
export async function closeSessionById(id: string, confirm = true): Promise<void> {
  const meta = useWorkspace.getState().sessions[id];
  const ctrl = controllers.get(id);
  if (confirm && ctrl && ctrl.state.status === 'connected' && meta?.kind !== 'files') {
    const ok = await dialogs.confirm({
      title: 'Close session',
      message: `Disconnect "${meta?.name ?? 'session'}"? The remote connection will be terminated.`,
      confirmLabel: 'Disconnect',
      danger: true,
    });
    if (!ok) return;
  }
  controllers.delete(id);
  useWorkspace.getState().update((l) => closeInLayout(l, id));
  useWorkspace.getState().forgetSession(id);
  await ctrl?.close();
}

export async function disconnectAll(): Promise<void> {
  const ids = [...controllers.keys()];
  if (!ids.length) return;
  const ok = await dialogs.confirm({
    title: 'Disconnect all',
    message: `Terminate all ${ids.length} session(s)?`,
    confirmLabel: 'Disconnect all',
    danger: true,
  });
  if (!ok) return;
  for (const id of ids) await closeSessionById(id, false);
  await post('/api/v1/sessions/disconnect-all').catch(() => undefined);
}

// --------------------------------------------------------------------------- layout persistence

let saveTimer: number | null = null;
let preferences: Record<string, unknown> = {};

export async function loadPreferences(): Promise<Record<string, unknown>> {
  try {
    preferences = (await get<{ data: Record<string, unknown> }>('/api/v1/me/preferences')).data ?? {};
  } catch {
    preferences = {};
  }
  return preferences;
}

export async function savePreference(key: string, value: unknown): Promise<void> {
  preferences = { ...preferences, [key]: value };
  await put('/api/v1/me/preferences', { data: preferences }).catch(() => undefined);
}

export function startLayoutPersistence(): () => void {
  return useWorkspace.subscribe((state, prev) => {
    if (state.layout === prev.layout) return;
    if (saveTimer !== null) window.clearTimeout(saveTimer);
    saveTimer = window.setTimeout(() => {
      const { layout, sessions } = useWorkspace.getState();
      const saved = toSaved(layout, (sid) => sessions[sid]?.connectionId);
      void savePreference('workspace', saved);
    }, 1500);
  });
}

export function savedWorkspace(): SavedLayout | null {
  const w = preferences.workspace as SavedLayout | null | undefined;
  return w && w.version === 1 ? w : null;
}

/** Reopen the sessions of the previous workspace and rebuild its layout. */
export async function restoreWorkspace(saved: SavedLayout, names: Map<string, ConnectionRef>): Promise<number> {
  // The same connection may have been open several times: open one new session per occurrence.
  const queues = new Map<string, string[]>();
  let opened = 0;
  for (const cid of savedConnectionIds(saved)) {
    const conn = names.get(cid);
    if (!conn) continue;
    const sid = await openConnection(conn);
    if (!sid) continue;
    queues.set(cid, [...(queues.get(cid) ?? []), sid]);
    opened += 1;
  }
  if (opened) useWorkspace.getState().replaceLayout(fromSaved(saved, (cid) => queues.get(cid)?.shift()));
  return opened;
}
