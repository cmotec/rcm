import { create } from 'zustand';
import type { Protocol, SecurityWarning, SessionKind } from '../api/types';
import { type Layout, type Rect, emptyLayout, openSession } from '../workspace/model';

export interface SessionMeta {
  id: string;
  connectionId: string;
  name: string;
  protocol: Protocol;
  kind: SessionKind;
  host: string | null;
  warnings: SecurityWarning[];
}

interface WorkspaceState {
  layout: Layout;
  sessions: Record<string, SessionMeta>;
  update: (fn: (l: Layout) => Layout) => void;
  addSession: (meta: SessionMeta, opts?: { floating?: Rect }) => void;
  forgetSession: (id: string) => void;
  replaceLayout: (layout: Layout) => void;
  /** Tabs (by id) in which keyboard input goes to every pane ("type in all panes"). */
  broadcast: Record<string, boolean>;
  toggleBroadcast: (tabId: string) => void;
  /** Session shown over the whole app (no menus, title bar or tabs), null = normal layout. */
  fullscreenSessionId: string | null;
  setFullscreen: (id: string | null) => void;
}

export const useWorkspace = create<WorkspaceState>((set) => ({
  layout: emptyLayout(),
  sessions: {},
  update: (fn) => set((s) => ({ layout: fn(s.layout) })),
  addSession: (meta, opts) =>
    set((s) => ({ sessions: { ...s.sessions, [meta.id]: meta }, layout: openSession(s.layout, meta.id, opts) })),
  forgetSession: (id) =>
    set((s) => {
      const sessions = { ...s.sessions };
      delete sessions[id];
      return { sessions, fullscreenSessionId: s.fullscreenSessionId === id ? null : s.fullscreenSessionId };
    }),
  replaceLayout: (layout) => set({ layout }),
  broadcast: {},
  toggleBroadcast: (tabId) => set((s) => ({ broadcast: { ...s.broadcast, [tabId]: !s.broadcast[tabId] } })),
  fullscreenSessionId: null,
  setFullscreen: (id) => set({ fullscreenSessionId: id }),
}));
