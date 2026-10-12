/**
 * Workspace layout model (pure functions, no React).
 *
 * - Tabs fill the workspace; each tab holds a layout tree (a single session, or splits).
 * - Floating windows overlap the tab area; they can be minimised to the taskbar.
 * - A session appears in exactly one place (a tab's tree or a floating window).
 */

export type SplitDir = 'row' | 'column';
export type Edge = 'left' | 'right' | 'top' | 'bottom';

export type LayoutNode =
  | { kind: 'leaf'; sessionId: string }
  | { kind: 'split'; id: string; dir: SplitDir; children: LayoutNode[]; sizes: number[] };

export interface Tab {
  id: string;
  root: LayoutNode;
}

export interface FloatingWindow {
  id: string;
  sessionId: string;
  x: number;
  y: number;
  w: number;
  h: number;
  z: number;
  minimized: boolean;
  maximized: boolean;
}

export interface Layout {
  tabs: Tab[];
  activeTabId: string | null;
  floats: FloatingWindow[];
  zTop: number;
  focusedSessionId: string | null;
}

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

let counter = 0;
export const newId = (prefix: string): string => `${prefix}-${Date.now().toString(36)}-${(counter++).toString(36)}`;

export const emptyLayout = (): Layout => ({ tabs: [], activeTabId: null, floats: [], zTop: 1, focusedSessionId: null });

// --------------------------------------------------------------------------- queries

export function leaves(node: LayoutNode): string[] {
  return node.kind === 'leaf' ? [node.sessionId] : node.children.flatMap(leaves);
}

export function allSessionIds(layout: Layout): string[] {
  return [...layout.tabs.flatMap((t) => leaves(t.root)), ...layout.floats.map((f) => f.sessionId)];
}

export type Placement =
  | { where: 'tab'; tabId: string; split: boolean }
  | { where: 'float'; floatId: string; minimized: boolean }
  | { where: 'none' };

export function placementOf(layout: Layout, sessionId: string): Placement {
  for (const t of layout.tabs) {
    if (leaves(t.root).includes(sessionId)) return { where: 'tab', tabId: t.id, split: t.root.kind === 'split' };
  }
  const f = layout.floats.find((x) => x.sessionId === sessionId);
  if (f) return { where: 'float', floatId: f.id, minimized: f.minimized };
  return { where: 'none' };
}

/** Sessions currently visible on screen (active tab + non-minimised floats). */
export function visibleSessionIds(layout: Layout): Set<string> {
  const out = new Set<string>();
  const tab = layout.tabs.find((t) => t.id === layout.activeTabId);
  if (tab) leaves(tab.root).forEach((s) => out.add(s));
  layout.floats.filter((f) => !f.minimized).forEach((f) => out.add(f.sessionId));
  return out;
}

// --------------------------------------------------------------------------- tree helpers

function normalize(node: LayoutNode): LayoutNode | null {
  if (node.kind === 'leaf') return node;
  const children: LayoutNode[] = [];
  const sizes: number[] = [];
  node.children.forEach((c, i) => {
    const n = normalize(c);
    if (!n) return;
    // flatten nested splits with the same direction
    if (n.kind === 'split' && n.dir === node.dir) {
      const total = n.sizes.reduce((a, b) => a + b, 0) || 1;
      n.children.forEach((cc, j) => {
        children.push(cc);
        sizes.push((node.sizes[i] ?? 1) * (n.sizes[j] / total));
      });
    } else {
      children.push(n);
      sizes.push(node.sizes[i] ?? 1);
    }
  });
  if (children.length === 0) return null;
  if (children.length === 1) return children[0];
  const total = sizes.reduce((a, b) => a + b, 0) || 1;
  return { ...node, children, sizes: sizes.map((s) => s / total) };
}

function removeLeaf(node: LayoutNode, sessionId: string): LayoutNode | null {
  if (node.kind === 'leaf') return node.sessionId === sessionId ? null : node;
  const kept: LayoutNode[] = [];
  const sizes: number[] = [];
  node.children.forEach((c, i) => {
    const r = removeLeaf(c, sessionId);
    if (r) {
      kept.push(r);
      sizes.push(node.sizes[i]);
    }
  });
  return normalize({ ...node, children: kept, sizes });
}

function insertBeside(node: LayoutNode, targetId: string, sessionId: string, edge: Edge): LayoutNode {
  if (node.kind === 'leaf') {
    if (node.sessionId !== targetId) return node;
    const dir: SplitDir = edge === 'left' || edge === 'right' ? 'row' : 'column';
    const fresh: LayoutNode = { kind: 'leaf', sessionId };
    const children = edge === 'left' || edge === 'top' ? [fresh, node] : [node, fresh];
    return { kind: 'split', id: newId('split'), dir, children, sizes: [0.5, 0.5] };
  }
  return normalize({ ...node, children: node.children.map((c) => insertBeside(c, targetId, sessionId, edge)) })!;
}

/** Remove a session from wherever it is (tabs or floats), keeping the rest of the layout intact. */
export function detach(layout: Layout, sessionId: string): Layout {
  const tabs: Tab[] = [];
  let activeTabId = layout.activeTabId;
  layout.tabs.forEach((t, idx) => {
    const root = removeLeaf(t.root, sessionId);
    if (root) tabs.push({ ...t, root });
    else if (t.id === activeTabId) {
      const neighbour = layout.tabs[idx + 1] ?? layout.tabs[idx - 1];
      activeTabId = neighbour && neighbour.id !== t.id ? neighbour.id : null;
    }
  });
  if (activeTabId && !tabs.some((t) => t.id === activeTabId)) activeTabId = tabs[0]?.id ?? null;
  return {
    ...layout,
    tabs,
    activeTabId,
    floats: layout.floats.filter((f) => f.sessionId !== sessionId),
    focusedSessionId: layout.focusedSessionId === sessionId ? null : layout.focusedSessionId,
  };
}

// --------------------------------------------------------------------------- actions

/** Open a session in a new tab that fills the workspace (or as a floating window). */
export function openSession(layout: Layout, sessionId: string, opts: { floating?: Rect } = {}): Layout {
  const base = detach(layout, sessionId);
  if (opts.floating) return addFloat(base, sessionId, opts.floating);
  const tab: Tab = { id: newId('tab'), root: { kind: 'leaf', sessionId } };
  return { ...base, tabs: [...base.tabs, tab], activeTabId: tab.id, focusedSessionId: sessionId };
}

export function closeSession(layout: Layout, sessionId: string): Layout {
  return detach(layout, sessionId);
}

export function activateTab(layout: Layout, tabId: string): Layout {
  const tab = layout.tabs.find((t) => t.id === tabId);
  if (!tab) return layout;
  return { ...layout, activeTabId: tabId, focusedSessionId: leaves(tab.root)[0] ?? null };
}

export function moveTab(layout: Layout, tabId: string, toIndex: number): Layout {
  const from = layout.tabs.findIndex((t) => t.id === tabId);
  if (from < 0) return layout;
  const tabs = [...layout.tabs];
  const [t] = tabs.splice(from, 1);
  tabs.splice(Math.max(0, Math.min(toIndex, tabs.length)), 0, t);
  return { ...layout, tabs };
}

function addFloat(layout: Layout, sessionId: string, rect: Rect): Layout {
  const z = layout.zTop + 1;
  const f: FloatingWindow = { id: newId('float'), sessionId, ...rect, z, minimized: false, maximized: false };
  return { ...layout, floats: [...layout.floats, f], zTop: z, focusedSessionId: sessionId };
}

/** Pop a session out of its tab/split into a floating window. */
export function floatSession(layout: Layout, sessionId: string, rect: Rect): Layout {
  return addFloat(detach(layout, sessionId), sessionId, rect);
}

/** Move a session (from anywhere) into its own tab. */
export function dockToTab(layout: Layout, sessionId: string, index?: number): Layout {
  const base = detach(layout, sessionId);
  const tab: Tab = { id: newId('tab'), root: { kind: 'leaf', sessionId } };
  const tabs = [...base.tabs];
  tabs.splice(index ?? tabs.length, 0, tab);
  return { ...base, tabs, activeTabId: tab.id, focusedSessionId: sessionId };
}

/** Place ``sessionId`` beside ``targetId`` (which must be in a tab), creating or extending a split. */
export function splitWith(layout: Layout, targetId: string, sessionId: string, edge: Edge): Layout {
  if (targetId === sessionId) return layout;
  const base = detach(layout, sessionId);
  const p = placementOf(base, targetId);
  if (p.where !== 'tab') return layout;
  const tabs = base.tabs.map((t) => (t.id === p.tabId ? { ...t, root: insertBeside(t.root, targetId, sessionId, edge) } : t));
  return { ...base, tabs, activeTabId: p.tabId, focusedSessionId: sessionId };
}

export function resizeSplit(layout: Layout, splitId: string, sizes: number[]): Layout {
  const total = sizes.reduce((a, b) => a + b, 0) || 1;
  const norm = sizes.map((s) => s / total);
  const walk = (n: LayoutNode): LayoutNode =>
    n.kind === 'leaf' ? n : n.id === splitId ? { ...n, sizes: norm } : { ...n, children: n.children.map(walk) };
  return { ...layout, tabs: layout.tabs.map((t) => ({ ...t, root: walk(t.root) })) };
}

function updateFloat(layout: Layout, floatId: string, fn: (f: FloatingWindow) => FloatingWindow): Layout {
  return { ...layout, floats: layout.floats.map((f) => (f.id === floatId ? fn(f) : f)) };
}

export function raiseFloat(layout: Layout, floatId: string): Layout {
  const f = layout.floats.find((x) => x.id === floatId);
  if (!f) return layout;
  if (f.z === layout.zTop && !f.minimized) return { ...layout, focusedSessionId: f.sessionId };
  const z = layout.zTop + 1;
  return { ...updateFloat(layout, floatId, (x) => ({ ...x, z, minimized: false })), zTop: z, focusedSessionId: f.sessionId };
}

export function moveFloat(layout: Layout, floatId: string, x: number, y: number): Layout {
  return updateFloat(layout, floatId, (f) => ({ ...f, x, y, maximized: false }));
}

export function resizeFloat(layout: Layout, floatId: string, rect: Rect): Layout {
  return updateFloat(layout, floatId, (f) => ({
    ...f,
    ...rect,
    w: Math.max(240, rect.w),
    h: Math.max(160, rect.h),
    maximized: false,
  }));
}

export function minimizeFloat(layout: Layout, floatId: string): Layout {
  const f = layout.floats.find((x) => x.id === floatId);
  const next = updateFloat(layout, floatId, (x) => ({ ...x, minimized: true }));
  return f && next.focusedSessionId === f.sessionId ? { ...next, focusedSessionId: null } : next;
}

export function restoreFloat(layout: Layout, floatId: string): Layout {
  return raiseFloat(updateFloat(layout, floatId, (f) => ({ ...f, minimized: false })), floatId);
}

export function toggleMaximizeFloat(layout: Layout, floatId: string): Layout {
  return raiseFloat(updateFloat(layout, floatId, (f) => ({ ...f, maximized: !f.maximized })), floatId);
}

export function focusSession(layout: Layout, sessionId: string): Layout {
  const p = placementOf(layout, sessionId);
  if (p.where === 'float') return raiseFloat(layout, p.floatId);
  if (p.where === 'tab') return { ...layout, activeTabId: p.tabId, focusedSessionId: sessionId };
  return layout;
}

/** Keep floating windows inside the workspace after the viewport shrinks. */
export function clampFloats(layout: Layout, bounds: { w: number; h: number }): Layout {
  let changed = false;
  const floats = layout.floats.map((f) => {
    const w = Math.min(f.w, Math.max(240, bounds.w));
    const h = Math.min(f.h, Math.max(160, bounds.h));
    const x = Math.min(Math.max(0, f.x), Math.max(0, bounds.w - 80));
    const y = Math.min(Math.max(0, f.y), Math.max(0, bounds.h - 40));
    if (w !== f.w || h !== f.h || x !== f.x || y !== f.y) changed = true;
    return { ...f, x, y, w, h };
  });
  return changed ? { ...layout, floats } : layout;
}

// --------------------------------------------------------------------------- persistence

/** Serializable layout where session ids are replaced by connection ids (for restoring after reload). */
export interface SavedLayout {
  version: 1;
  tabs: { root: LayoutNode }[];
  floats: Omit<FloatingWindow, 'id' | 'z'>[];
  activeIndex: number;
}

export function toSaved(layout: Layout, connectionOf: (sessionId: string) => string | undefined): SavedLayout | null {
  const map = (n: LayoutNode): LayoutNode | null => {
    if (n.kind === 'leaf') {
      const c = connectionOf(n.sessionId);
      return c ? { kind: 'leaf', sessionId: c } : null;
    }
    return normalize({ ...n, children: n.children.map(map).filter((x): x is LayoutNode => x !== null) });
  };
  const tabs = layout.tabs.map((t) => map(t.root)).filter((r): r is LayoutNode => r !== null).map((root) => ({ root }));
  const floats = layout.floats
    .map((f) => ({ ...f, sessionId: connectionOf(f.sessionId) }))
    .filter((f): f is FloatingWindow & { sessionId: string } => Boolean(f.sessionId))
    .map(({ id: _id, z: _z, ...rest }) => rest);
  if (!tabs.length && !floats.length) return null;
  return { version: 1, tabs, floats, activeIndex: Math.max(0, layout.tabs.findIndex((t) => t.id === layout.activeTabId)) };
}

/** Rebuild a layout from a saved one, mapping connection ids to newly opened session ids. */
export function fromSaved(saved: SavedLayout, sessionFor: (connectionId: string) => string | undefined): Layout {
  const map = (n: LayoutNode): LayoutNode | null => {
    if (n.kind === 'leaf') {
      const s = sessionFor(n.sessionId);
      return s ? { kind: 'leaf', sessionId: s } : null;
    }
    return normalize({ ...n, id: newId('split'), children: n.children.map(map).filter((x): x is LayoutNode => x !== null) });
  };
  const tabs: Tab[] = saved.tabs
    .map((t) => map(t.root))
    .filter((r): r is LayoutNode => r !== null)
    .map((root) => ({ id: newId('tab'), root }));
  let z = 1;
  const floats: FloatingWindow[] = saved.floats
    .map((f) => ({ ...f, sessionId: sessionFor(f.sessionId) }))
    .filter((f): f is typeof f & { sessionId: string } => Boolean(f.sessionId))
    .map((f) => ({ ...f, id: newId('float'), z: ++z }));
  return { tabs, floats, activeTabId: tabs[Math.min(saved.activeIndex, tabs.length - 1)]?.id ?? null, zTop: z, focusedSessionId: null };
}

/** Connection ids referenced by a saved layout, in display order. */
export function savedConnectionIds(saved: SavedLayout): string[] {
  return [...saved.tabs.flatMap((t) => leaves(t.root)), ...saved.floats.map((f) => f.sessionId)];
}
