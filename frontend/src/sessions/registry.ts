import { ApiError, del, errorMessage, get, post, put } from '../api/client';
import type { HostKeyInfo, Protocol, SessionInfo } from '../api/types';
import { type AuthOverride, dialogs, type InteractiveCredentials } from '../state/dialogs';
import { beginConnect, type ConnectHandle } from '../state/connecting';
import { useWorkspace } from '../state/workspace';
import {
  type Edge,
  type Rect,
  type SavedLayout,
  closeSession as closeInLayout,
  fromSaved,
  leaves,
  placementOf,
  savedConnectionIds,
  splitWith,
  toSaved,
} from '../workspace/model';
import { BaseSession as BaseSessionClass, type BaseSession } from './base';
import { applySessionPrefs } from './prefs';
import { FileSession } from './FileSession';
import { GuacSession } from './GuacSession';
import { TerminalSession } from './TerminalSession';

/** Live session controllers, keyed by session id (kept outside React on purpose). */
const controllers = new Map<string, BaseSession>();

/** Other panes of the same tab, while "type in all panes" is on for that tab. */
BaseSessionClass.broadcastTargets = (sourceId: string): BaseSession[] => {
  const { layout, broadcast } = useWorkspace.getState();
  const placement = placementOf(layout, sourceId);
  if (placement.where !== 'tab' || !broadcast[placement.tabId]) return [];
  const tab = layout.tabs.find((t) => t.id === placement.tabId);
  if (!tab) return [];
  return leaves(tab.root)
    .filter((id) => id !== sourceId)
    .map((id) => controllers.get(id))
    .filter((c): c is BaseSession => c !== undefined);
};

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
  /** Different credentials for this session only (replaces the configured authentication). */
  authOverride?: AuthOverride;
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
  const loading = beginConnect(conn.name, conn.protocol);
  try {
    return await startSession(conn, target, loading);
  } finally {
    loading.end();
  }
}

/** Ask the user something while the loading card is hidden. */
async function ask<T>(loading: ConnectHandle, question: () => Promise<T>): Promise<T> {
  loading.waiting(true);
  try {
    return await question();
  } finally {
    loading.waiting(false);
  }
}

async function startSession(conn: ConnectionRef, target: OpenTarget, loading: ConnectHandle): Promise<string | null> {
  let credentials: InteractiveCredentials | undefined;
  let credentialId: string | undefined;
  let authOverride = target.authOverride;
  const accepted: { hostname: string; port: number; fingerprint: string }[] = [];
  let info: SessionInfo | null = null;
  for (let attempt = 0; attempt < 8 && !info; attempt++) {
    if (loading.cancelled) return null;
    loading.step(attempt === 0 ? 'Preparing the connection…' : 'Starting the session…');
    try {
      info = await post<SessionInfo>('/api/v1/sessions', {
        connection_id: conn.id,
        credentials,
        credential_id: credentialId,
        accept_host_keys: accepted,
        auth_override: authOverride,
      });
    } catch (err) {
      if (loading.cancelled) return null; // given up meanwhile: no dialogs, no error toast
      if (err instanceof ApiError && err.code === 'credential_choice_required') {
        const problem = err.problem as { folder_id?: string | null; accounts_only?: boolean };
        const chosen = await ask(loading, () => dialogs.chooseCredential(conn.id, conn.name, conn.protocol,
          problem.folder_id ?? null, Boolean(problem.accounts_only)));
        if (!chosen) return null;
        if (chosen.mode === 'credential') credentialId = chosen.credential_id;
        else authOverride = chosen; // one of the user's accounts, or typed: this session only
        continue;
      }
      if (err instanceof ApiError && err.code === 'credentials_required') {
        const p = err.problem as unknown as { fields: string[]; username?: string; domain?: string };
        const entered = await ask(loading, () => dialogs.credentials({
          connectionName: conn.name,
          protocol: conn.protocol,
          fields: p.fields,
          username: credentials?.username ?? p.username,
          domain: credentials?.domain ?? p.domain,
        }));
        if (!entered) return null;
        if ('mode' in entered) authOverride = entered; // one of the user's own accounts
        else credentials = entered;
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
          await ask(loading, () => dialogs.hostKey(hk, true));
          return null;
        }
        const ok = await ask(loading, () => dialogs.hostKey(hk, changed));
        if (!ok) return null;
        accepted.push({ hostname: hk.hostname, port: hk.port, fingerprint: hk.fingerprint });
        continue;
      }
      dialogs.toast(`${conn.name}: ${errorMessage(err)}`, 'error');
      return null;
    }
  }
  if (!info) return null;
  if (loading.cancelled) {
    await del(`/api/v1/sessions/${info.id}`).catch(() => undefined);
    return null;
  }
  const ctrl = createController(info);
  controllers.set(info.id, ctrl);
  ctrl.onEnded = () => void closeSessionById(info!.id);
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
/** Close a session window: terminates the remote session without asking (closing is intentional). */
export async function closeSessionById(id: string): Promise<void> {
  const ctrl = controllers.get(id);
  controllers.delete(id);
  useWorkspace.getState().update((l) => closeInLayout(l, id));
  useWorkspace.getState().forgetSession(id);
  await ctrl?.close();
}

/**
 * Hand a session over to another browser window: drop it here (layout and local connection) without
 * ending it on the server, so the other window can attach to it.
 */
export function releaseSession(id: string): void {
  const ctrl = controllers.get(id);
  controllers.delete(id);
  useWorkspace.getState().update((l) => closeInLayout(l, id));
  useWorkspace.getState().forgetSession(id);
  ctrl?.dispose();
}

/** Show an existing server session in this window (the "new window" page attaches to it). */
export function adoptSession(info: SessionInfo, onGone: () => void): BaseSession {
  const ctrl = createController({ ...info, ticket: null }); // no ticket: the controller asks for a fresh one
  controllers.set(info.id, ctrl);
  ctrl.onEnded = () => {
    void closeSessionById(info.id);
    onGone();
  };
  useWorkspace.getState().addSession({
    id: info.id,
    connectionId: info.connection_id ?? '',
    name: info.connection_name,
    protocol: info.protocol,
    kind: info.kind,
    host: info.effective_host,
    warnings: info.warnings,
  });
  return ctrl;
}

/** Open a session in its own browser window (e.g. for a second monitor); the session keeps running. */
export function openInNewWindow(id: string): void {
  const ctrl = controllers.get(id);
  const rect = ctrl?.element.getBoundingClientRect();
  const w = Math.round(Math.max(640, Math.min(window.screen.availWidth, rect?.width ?? 1280)));
  const h = Math.round(Math.max(400, Math.min(window.screen.availHeight, (rect?.height ?? 800) + 40)));
  const win = window.open(`/popup/session/${encodeURIComponent(id)}`, `session-${id}`,
    `popup=yes,width=${w},height=${h}`);
  if (!win) {
    dialogs.toast('The browser blocked the new window. Allow pop-ups for this site and try again.', 'warning');
    return;
  }
  releaseSession(id);
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
  for (const id of ids) await closeSessionById(id);
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
  applySessionPrefs(preferences);
  return preferences;
}

export async function savePreference(key: string, value: unknown): Promise<void> {
  preferences = { ...preferences, [key]: value };
  applySessionPrefs(preferences);
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
