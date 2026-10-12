import { AppWindow, Maximize2, Minimize2, Minus, PanelTop, X } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import type { Protocol } from '../api/types';
import { ProtocolIcon } from '../components/common/icons';
import { closeSessionById, getController, openConnection } from '../sessions/registry';
import { dialogs } from '../state/dialogs';
import { useWorkspace } from '../state/workspace';
import {
  type Edge,
  type FloatingWindow,
  type LayoutNode,
  type Tab,
  activateTab,
  clampFloats,
  dockToTab,
  floatSession,
  leaves,
  minimizeFloat,
  moveFloat,
  moveTab,
  placementOf,
  raiseFloat,
  resizeFloat,
  resizeSplit,
  restoreFloat,
  splitWith,
  toggleMaximizeFloat,
} from './model';
import { SessionFrame } from './SessionFrame';
import { CONNECTION_MIME, SESSION_MIME, useSessionState } from './useSession';

// --------------------------------------------------------------------------- tab strip

function TabStatus({ sessionId }: { sessionId: string }) {
  const s = useSessionState(sessionId);
  return <span className={`status-dot ${s.status}`} title={s.message ?? s.status} />;
}

function TabButton({ tab, index, active }: { tab: Tab; index: number; active: boolean }) {
  const sessions = useWorkspace((s) => s.sessions);
  const update = useWorkspace((s) => s.update);
  const ids = leaves(tab.root);
  const first = sessions[ids[0]];
  const title = ids.map((id) => sessions[id]?.name ?? '…').join(' | ');
  const close = async () => {
    for (const id of ids) await closeSessionById(id);
  };
  return (
    <div className={`tab ${active ? 'active' : ''}`} role="tab" aria-selected={active}
         draggable={ids.length === 1}
         onDragStart={(e) => {
           e.dataTransfer.setData(SESSION_MIME, ids[0]);
           e.dataTransfer.setData('application/x-tab-index', String(index));
           e.dataTransfer.effectAllowed = 'move';
         }}
         onClick={() => {
           update((l) => activateTab(l, tab.id));
           getController(ids[0])?.focus();
         }}
         onAuxClick={(e) => e.button === 1 && void close()}
         onContextMenu={(e) => {
           e.preventDefault();
           dialogs.contextMenu(e.clientX, e.clientY, [
             ...(ids.length === 1 ? [{ label: 'Open in floating window', action: () => update((l) => floatSession(l, ids[0], { x: 60, y: 40, w: 900, h: 600 })) }] : []),
             { label: 'Reconnect', action: () => ids.forEach((id) => void getController(id)?.reconnect(true)) },
             { separator: true, label: '' },
             { label: ids.length > 1 ? 'Close all in tab' : 'Disconnect', danger: true, action: () => void close() },
           ]);
         }}
         title={title}>
      {first && <ProtocolIcon protocol={first.protocol} size={13} />}
      <span className="tab-title">{title}</span>
      {ids.map((id) => <TabStatus key={id} sessionId={id} />)}
      <button className="icon-btn tab-close" aria-label="Close tab" onClick={(e) => { e.stopPropagation(); void close(); }}>
        <X size={12} />
      </button>
    </div>
  );
}

function TabStrip() {
  const { tabs, activeTabId } = useWorkspace((s) => s.layout);
  const update = useWorkspace((s) => s.update);
  const [over, setOver] = useState(false);
  const indexAt = (e: React.DragEvent) => {
    const els = Array.from((e.currentTarget as HTMLElement).querySelectorAll('.tab'));
    const i = els.findIndex((el) => e.clientX < el.getBoundingClientRect().left + el.getBoundingClientRect().width / 2);
    return i === -1 ? els.length : i;
  };
  return (
    <div className={`tab-strip ${over ? 'drop-target' : ''}`} role="tablist"
         onDragOver={(e) => {
           if (!e.dataTransfer.types.includes(SESSION_MIME)) return;
           e.preventDefault();
           setOver(true);
         }}
         onDragLeave={() => setOver(false)}
         onDrop={(e) => {
           setOver(false);
           const sid = e.dataTransfer.getData(SESSION_MIME);
           if (!sid) return;
           e.preventDefault();
           const to = indexAt(e);
           const fromTab = e.dataTransfer.getData('application/x-tab-index');
           update((l) => {
             const p = placementOf(l, sid);
             if (fromTab !== '' && p.where === 'tab' && !p.split) return moveTab(l, p.tabId, to > Number(fromTab) ? to - 1 : to);
             return dockToTab(l, sid, to);
           });
         }}>
      {tabs.map((t, i) => <TabButton key={t.id} tab={t} index={i} active={t.id === activeTabId} />)}
    </div>
  );
}

// --------------------------------------------------------------------------- split view

function Divider({ splitId, dir, index, sizes }: { splitId: string; dir: 'row' | 'column'; index: number; sizes: number[] }) {
  const update = useWorkspace((s) => s.update);
  const onPointerDown = (e: React.PointerEvent) => {
    e.preventDefault();
    const container = (e.currentTarget as HTMLElement).parentElement!;
    const rect = container.getBoundingClientRect();
    const total = dir === 'row' ? rect.width : rect.height;
    const start = dir === 'row' ? e.clientX : e.clientY;
    const initial = [...sizes];
    const target = e.currentTarget as HTMLElement;
    target.setPointerCapture(e.pointerId);
    document.body.classList.add(dir === 'row' ? 'resizing-col' : 'resizing-row');
    const move = (ev: PointerEvent) => {
      const delta = ((dir === 'row' ? ev.clientX : ev.clientY) - start) / total;
      const a = initial[index] + delta;
      const b = initial[index + 1] - delta;
      const min = 0.08;
      if (a < min || b < min) return;
      const next = [...initial];
      next[index] = a;
      next[index + 1] = b;
      update((l) => resizeSplit(l, splitId, next));
    };
    const up = () => {
      target.removeEventListener('pointermove', move);
      target.removeEventListener('pointerup', up);
      document.body.classList.remove('resizing-col', 'resizing-row');
    };
    target.addEventListener('pointermove', move);
    target.addEventListener('pointerup', up);
  };
  return <div className={`divider divider-${dir}`} onPointerDown={onPointerDown} role="separator" />;
}

function LayoutView({ node, inSplit }: { node: LayoutNode; inSplit: boolean }) {
  if (node.kind === 'leaf') return <SessionFrame sessionId={node.sessionId} mode={inSplit ? 'split' : 'tab'} />;
  return (
    <div className={`split split-${node.dir}`}>
      {node.children.map((child, i) => (
        <div key={child.kind === 'leaf' ? child.sessionId : child.id} className="split-cell"
             style={{ flexGrow: node.sizes[i], flexBasis: 0 }}>
          <LayoutView node={child} inSplit />
          {i < node.children.length - 1 && <Divider splitId={node.id} dir={node.dir} index={i} sizes={node.sizes} />}
        </div>
      ))}
    </div>
  );
}

// --------------------------------------------------------------------------- floating windows

type ResizeDir = 'n' | 's' | 'e' | 'w' | 'ne' | 'nw' | 'se' | 'sw';

function FloatingFrame({ win }: { win: FloatingWindow }) {
  const update = useWorkspace((s) => s.update);

  const beginMove = (e: React.PointerEvent) => {
    if (e.button !== 0 || win.maximized) return;
    const el = e.currentTarget as HTMLElement;
    el.setPointerCapture(e.pointerId);
    const sx = e.clientX;
    const sy = e.clientY;
    const ox = win.x;
    const oy = win.y;
    const move = (ev: PointerEvent) => update((l) => moveFloat(l, win.id, Math.max(0, ox + ev.clientX - sx), Math.max(0, oy + ev.clientY - sy)));
    const up = (ev: PointerEvent) => {
      el.removeEventListener('pointermove', move);
      el.removeEventListener('pointerup', up);
      // dropping the window onto the tab bar docks it
      const target = document.elementFromPoint(ev.clientX, ev.clientY);
      if (target?.closest('.tab-strip')) update((l) => dockToTab(l, win.sessionId));
    };
    el.addEventListener('pointermove', move);
    el.addEventListener('pointerup', up);
  };

  const beginResize = (dir: ResizeDir) => (e: React.PointerEvent) => {
    e.preventDefault();
    e.stopPropagation();
    const el = e.currentTarget as HTMLElement;
    el.setPointerCapture(e.pointerId);
    const sx = e.clientX;
    const sy = e.clientY;
    const o = { x: win.x, y: win.y, w: win.w, h: win.h };
    const move = (ev: PointerEvent) => {
      const dx = ev.clientX - sx;
      const dy = ev.clientY - sy;
      const r = { ...o };
      if (dir.includes('e')) r.w = o.w + dx;
      if (dir.includes('s')) r.h = o.h + dy;
      if (dir.includes('w')) {
        r.w = o.w - dx;
        r.x = o.x + dx;
      }
      if (dir.includes('n')) {
        r.h = o.h - dy;
        r.y = o.y + dy;
      }
      update((l) => resizeFloat(l, win.id, r));
    };
    const up = () => {
      el.removeEventListener('pointermove', move);
      el.removeEventListener('pointerup', up);
    };
    el.addEventListener('pointermove', move);
    el.addEventListener('pointerup', up);
  };

  const style = win.maximized
    ? { left: 0, top: 0, right: 0, bottom: 0, zIndex: 10 + win.z }
    : { left: win.x, top: win.y, width: win.w, height: win.h, zIndex: 10 + win.z };

  return (
    <div className={`floating ${win.maximized ? 'maximized' : ''}`} style={style} data-float={win.id}
         onMouseDownCapture={() => update((l) => raiseFloat(l, win.id))}>
      <SessionFrame
        sessionId={win.sessionId}
        mode="float"
        onTitlePointerDown={beginMove}
        onTitleDoubleClick={() => update((l) => toggleMaximizeFloat(l, win.id))}
        floatControls={<>
          <button className="icon-btn" title="Minimise" onClick={() => update((l) => minimizeFloat(l, win.id))}><Minus size={14} /></button>
          <button className="icon-btn" title={win.maximized ? 'Restore' : 'Maximise'} onClick={() => update((l) => toggleMaximizeFloat(l, win.id))}>
            {win.maximized ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
          </button>
          <button className="icon-btn" title="Dock as tab" onClick={() => update((l) => dockToTab(l, win.sessionId))}><PanelTop size={14} /></button>
        </>}
      />
      {!win.maximized && (['n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw'] as ResizeDir[]).map((d) => (
        <div key={d} className={`rz rz-${d}`} onPointerDown={beginResize(d)} />
      ))}
    </div>
  );
}

function Taskbar() {
  const floats = useWorkspace((s) => s.layout.floats);
  const sessions = useWorkspace((s) => s.sessions);
  const update = useWorkspace((s) => s.update);
  if (!floats.length) return null;
  return (
    <div className="taskbar">
      <AppWindow size={14} className="muted" />
      {floats.map((f) => {
        const m = sessions[f.sessionId];
        return (
          <button key={f.id} className={`taskbar-item ${f.minimized ? 'minimized' : ''}`}
                  onClick={() => update((l) => (f.minimized ? restoreFloat(l, f.id) : raiseFloat(l, f.id)))}
                  title={f.minimized ? 'Restore' : 'Bring to front'}>
            {m && <ProtocolIcon protocol={m.protocol} size={12} />}
            <span>{m?.name ?? 'session'}</span>
            <TabStatus sessionId={f.sessionId} />
          </button>
        );
      })}
    </div>
  );
}

// --------------------------------------------------------------------------- drop zones

interface DropTarget {
  mode: Edge | 'float';
  targetId?: string;
  box: { left: number; top: number; width: number; height: number };
}

function computeDrop(e: React.DragEvent, body: HTMLElement): DropTarget {
  const bodyRect = body.getBoundingClientRect();
  const leafEl = document
    .elementsFromPoint(e.clientX, e.clientY)
    .find((el): el is HTMLElement => el instanceof HTMLElement && Boolean(el.dataset.leaf));
  if (leafEl) {
    const r = leafEl.getBoundingClientRect();
    const fx = (e.clientX - r.left) / r.width;
    const fy = (e.clientY - r.top) / r.height;
    const d = { left: fx, right: 1 - fx, top: fy, bottom: 1 - fy };
    const [edge, dist] = (Object.entries(d) as [Edge, number][]).sort((a, b) => a[1] - b[1])[0];
    const rel = { left: r.left - bodyRect.left, top: r.top - bodyRect.top, width: r.width, height: r.height };
    if (dist < 0.25) {
      const box = { ...rel };
      if (edge === 'left') box.width /= 2;
      if (edge === 'right') { box.left += box.width / 2; box.width /= 2; }
      if (edge === 'top') box.height /= 2;
      if (edge === 'bottom') { box.top += box.height / 2; box.height /= 2; }
      return { mode: edge, targetId: leafEl.dataset.leaf, box };
    }
  }
  const w = Math.min(900, bodyRect.width * 0.6);
  const h = Math.min(600, bodyRect.height * 0.6);
  const left = Math.max(0, Math.min(e.clientX - bodyRect.left - w / 2, bodyRect.width - w));
  const top = Math.max(0, Math.min(e.clientY - bodyRect.top - 16, bodyRect.height - h));
  return { mode: 'float', box: { left, top, width: w, height: h } };
}

// --------------------------------------------------------------------------- workspace

export function Workspace({ emptyHint }: { emptyHint?: React.ReactNode }) {
  const layout = useWorkspace((s) => s.layout);
  const update = useWorkspace((s) => s.update);
  const body = useRef<HTMLDivElement>(null);
  const [drop, setDrop] = useState<DropTarget | null>(null);
  const activeTab = layout.tabs.find((t) => t.id === layout.activeTabId) ?? null;

  useEffect(() => {
    const el = body.current;
    if (!el) return undefined;
    const ro = new ResizeObserver(() => update((l) => clampFloats(l, { w: el.clientWidth, h: el.clientHeight })));
    ro.observe(el);
    return () => ro.disconnect();
  }, [update]);

  // focus follows the active tab
  useEffect(() => {
    if (layout.focusedSessionId) getController(layout.focusedSessionId)?.focus();
  }, [layout.focusedSessionId, layout.activeTabId]);

  const onDrop = useCallback(
    (e: React.DragEvent) => {
      const sid = e.dataTransfer.getData(SESSION_MIME);
      const connRaw = e.dataTransfer.getData(CONNECTION_MIME);
      setDrop(null);
      if (!body.current) return;
      if (!sid && connRaw) {
        e.preventDefault();
        const conn = JSON.parse(connRaw) as { id: string; name: string; protocol: Protocol };
        if (conn.protocol === 'host') return;
        const target = computeDrop(e, body.current);
        if (target.mode === 'float') {
          void openConnection(conn, { floating: { x: target.box.left, y: target.box.top, w: target.box.width, h: target.box.height } });
        } else if (target.targetId) {
          void openConnection(conn, { splitWith: { sessionId: target.targetId, edge: target.mode as Edge } });
        }
        return;
      }
      if (!sid) return;
      e.preventDefault();
      const target = computeDrop(e, body.current);
      if (target.mode === 'float') {
        const rect = { x: target.box.left, y: target.box.top, w: target.box.width, h: target.box.height };
        update((l) => {
          const p = placementOf(l, sid);
          return p.where === 'float' ? moveFloat(l, p.floatId, rect.x, rect.y) : floatSession(l, sid, rect);
        });
      } else if (target.targetId && target.targetId !== sid) {
        update((l) => splitWith(l, target.targetId!, sid, target.mode as Edge));
      }
    },
    [update],
  );

  return (
    <div className="workspace">
      <TabStrip />
      <div ref={body} className="workspace-body"
           onDragOver={(e) => {
             const types = e.dataTransfer.types;
             if ((!types.includes(SESSION_MIME) && !types.includes(CONNECTION_MIME)) || !body.current) return;
             e.preventDefault();
             setDrop(computeDrop(e, body.current));
           }}
           onDragLeave={(e) => {
             if (!body.current?.contains(e.relatedTarget as Node)) setDrop(null);
           }}
           onDrop={onDrop}>
        {activeTab ? <LayoutView key={activeTab.id} node={activeTab.root} inSplit={false} /> : !layout.floats.length && (
          <div className="workspace-empty">{emptyHint}</div>
        )}
        {layout.floats.filter((f) => !f.minimized).map((f) => <FloatingFrame key={f.id} win={f} />)}
        {drop && (
          <div className={`drop-zone ${drop.mode}`} style={drop.box}>
            <span>{drop.mode === 'float' ? 'Floating window' : `Split ${drop.mode}`}</span>
          </div>
        )}
      </div>
      <Taskbar />
    </div>
  );
}
