import { Check, ChevronDown, ChevronRight, FolderPlus, Plus, Search, ShieldAlert, SlidersHorizontal, Star, X } from 'lucide-react';
import { useMemo, useState } from 'react';
import { del, errorMessage, patch, post, put } from '../../api/client';
import { hasPerm, useInvalidate, useMe, useTree } from '../../api/hooks';
import type { Connection, Folder } from '../../api/types';
import { openConnection } from '../../sessions/registry';
import { dialogs } from '../../state/dialogs';
import { connectionLabel, useTreePrefs } from '../../state/treePrefs';
import { useWorkspace } from '../../state/workspace';
import { FolderGlyph, ProtocolIcon } from '../common/icons';
import { type TreeNode, buildTree, filterTree } from './buildTree';

import { CONNECTION_MIME } from '../../workspace/useSession';

const NODE_MIME = 'application/x-tree-node';

export interface TreeActions {
  editConnection: (c: Connection) => void;
  newConnection: (where: { folderId?: string | null; parentId?: string | null }) => void;
  newFolder: (parentId: string | null) => void;
  editFolder: (f: Folder) => void;
  permissions: (type: 'folder' | 'connection', id: string, name: string) => void;
  select: (c: Connection | null) => void;
  selectedId: string | null;
}

const keyOf = (n: TreeNode) => `${n.type}:${n.id}`;

function loadExpanded(): Set<string> {
  try {
    return new Set(JSON.parse(localStorage.getItem('tree.expanded') ?? '[]') as string[]);
  } catch {
    return new Set();
  }
}

export function ConnectionTree({ actions }: { actions: TreeActions }) {
  const { data: tree, isLoading, error } = useTree();
  const { data: me } = useMe();
  const invalidate = useInvalidate();
  const [query, setQuery] = useState('');
  const [expanded, setExpanded] = useState<Set<string>>(loadExpanded);
  const [dropId, setDropId] = useState<string | null>(null);
  const focusedSession = useWorkspace((s) => s.layout.focusedSessionId);
  const prefs = useTreePrefs();
  const label = (c: Connection) => connectionLabel(c, prefs);
  const renderLabel = (c: Connection) => {
    const l = label(c);
    return (
      <span className="tree-name" title={l.secondary ? `${l.primary} (${l.secondary})` : l.primary}>
        {l.primary}{l.secondary && <span className="tree-secondary"> ({l.secondary})</span>}
      </span>
    );
  };
  const viewMenu = (e: React.MouseEvent) => {
    const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const tick = (on: boolean) => (on ? <Check size={13} /> : <span style={{ width: 13 }} />);
    dialogs.contextMenu(r.left, r.bottom + 4, [
      { label: 'Hostname first, description in ( )', icon: tick(prefs.order === 'hostname'), action: () => prefs.update({ order: 'hostname' }) },
      { label: 'Description first, hostname in ( )', icon: tick(prefs.order === 'description'), action: () => prefs.update({ order: 'description' }) },
      { separator: true, label: '' },
      { label: 'Hide hostname', icon: tick(prefs.hideHostname), action: () => prefs.update({ hideHostname: !prefs.hideHostname }) },
    ]);
  };

  const [selection, setSelection] = useState<Set<string>>(new Set());
  const [anchor, setAnchor] = useState<string | null>(null);

  const nodes = useMemo(() => (tree ? filterTree(buildTree(tree), query) : []), [tree, query]);
  const favorites = useMemo(() => tree?.connections.filter((c) => c.favorite) ?? [], [tree]);

  // Rows in display order (for Shift+click ranges and Ctrl+A).
  const visibleOrder = useMemo(() => {
    const out: string[] = [];
    const walk = (list: TreeNode[]) => {
      for (const n of list) {
        out.push(keyOf(n));
        if (n.children.length && (query || expanded.has(n.id))) walk(n.children);
      }
    };
    walk(nodes);
    return out;
  }, [nodes, expanded, query]);

  const toggle = (id: string) => {
    setExpanded((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      localStorage.setItem('tree.expanded', JSON.stringify([...n]));
      return n;
    });
  };

  const run = async (fn: () => Promise<unknown>, okMsg?: string) => {
    try {
      await fn();
      invalidate.tree();
      if (okMsg) dialogs.toast(okMsg, 'success');
    } catch (err) {
      dialogs.toast(errorMessage(err), 'error');
    }
  };

  const open = (c: Connection, mode: 'tab' | 'float' | 'split' = 'tab') => {
    if (c.protocol === 'host') {
      toggle(c.id);
      return;
    }
    const ref = { ...c, name: label(c).primary };  // tab titles use the same label as the tree
    if (mode === 'float') void openConnection(ref, { floating: { x: 60, y: 40, w: 960, h: 620 } });
    else if (mode === 'split' && focusedSession) void openConnection(ref, { splitWith: { sessionId: focusedSession, edge: 'right' } });
    else void openConnection(ref);
  };

  const deleteConnection = async (c: Connection, hasChildren: boolean) => {
    const ok = await dialogs.confirm({ title: 'Delete connection', danger: true, confirmLabel: 'Delete',
      message: `Delete "${c.name}"${hasChildren ? ' and all its child connections' : ''}?` });
    if (ok) await run(() => del(`/api/v1/connections/${c.id}`, { recursive: hasChildren }), 'Connection deleted');
  };

  const deleteFolder = async (f: Folder, hasChildren: boolean) => {
    const ok = await dialogs.confirm({ title: 'Delete folder', danger: true, confirmLabel: 'Delete',
      message: `Delete folder "${f.name}"${hasChildren ? ' including everything inside it' : ''}?` });
    if (ok) await run(() => del(`/api/v1/folders/${f.id}`, { recursive: hasChildren }), 'Folder deleted');
  };

  // ------------------------------------------------------------------ multi-select

  const onRowClick = (e: React.MouseEvent, n: TreeNode) => {
    const key = keyOf(n);
    if (e.shiftKey && anchor && visibleOrder.includes(anchor)) {
      const [a, b] = [visibleOrder.indexOf(anchor), visibleOrder.indexOf(key)].sort((x, y) => x - y);
      const range = visibleOrder.slice(a, b + 1);
      setSelection((s) => new Set(e.ctrlKey || e.metaKey ? [...s, ...range] : range));
      return;
    }
    if (e.ctrlKey || e.metaKey) {
      setSelection((s) => {
        const next = new Set(s);
        if (next.has(key)) next.delete(key);
        else next.add(key);
        return next;
      });
      setAnchor(key);
      return;
    }
    setSelection(new Set([key]));
    setAnchor(key);
    if (n.type === 'folder') toggle(n.id);
    else actions.select(n.conn);
  };

  /** Delete the given rows. Items below another selected item are skipped (deleted with their parent). */
  const deleteMany = async (keys: string[]) => {
    if (!tree) return;
    const folders = new Map(tree.folders.map((f) => [f.id, f]));
    const conns = new Map(tree.connections.map((c) => [c.id, c]));
    const chosen = new Set(keys);
    const ancestors = (key: string): string[] => {
      const out: string[] = [];
      const [type, id] = key.split(':');
      let folderId: string | null = null;
      if (type === 'connection') {
        let c = conns.get(id);
        while (c?.parent_id) {
          out.push(`connection:${c.parent_id}`);
          c = conns.get(c.parent_id);
        }
        folderId = c?.folder_id ?? null;
      } else {
        folderId = folders.get(id)?.parent_id ?? null;
      }
      while (folderId) {
        out.push(`folder:${folderId}`);
        folderId = folders.get(folderId)?.parent_id ?? null;
      }
      return out;
    };
    const roots = keys.filter((k) => !ancestors(k).some((a) => chosen.has(a)));
    const permitted = roots.filter((k) => {
      const [type, id] = k.split(':');
      return (type === 'folder' ? folders.get(id)?.permission : conns.get(id)?.permission) === 'manage';
    });
    const denied = roots.length - permitted.length;
    if (!permitted.length) {
      dialogs.toast('You are not allowed to delete the selected items', 'error');
      return;
    }
    const nFolders = permitted.filter((k) => k.startsWith('folder:')).length;
    const nConns = permitted.length - nFolders;
    const parts = [nFolders && `${nFolders} folder(s)`, nConns && `${nConns} connection(s)`].filter(Boolean).join(' and ');
    const ok = await dialogs.confirm({
      title: 'Delete selected items',
      danger: true,
      confirmLabel: `Delete ${permitted.length}`,
      message: `Delete ${parts}, including everything inside them?${denied ? ` ${denied} item(s) you may not delete will be skipped.` : ''}`,
    });
    if (!ok) return;
    const failures: string[] = [];
    // connections first: a folder delete would otherwise remove them and make their own delete fail
    for (const k of [...permitted].sort((a, b) => Number(a.startsWith('folder:')) - Number(b.startsWith('folder:')))) {
      const [type, id] = k.split(':');
      const name = type === 'folder' ? folders.get(id)?.name : conns.get(id)?.name;
      try {
        await del(`/api/v1/${type === 'folder' ? 'folders' : 'connections'}/${id}`, { recursive: true });
      } catch (err) {
        failures.push(`${name}: ${errorMessage(err)}`);
      }
    }
    setSelection(new Set());
    invalidate.tree();
    const deleted = permitted.length - failures.length;
    if (deleted) dialogs.toast(`Deleted ${deleted} item(s)`, 'success');
    if (failures.length) dialogs.toast(`Could not delete: ${failures.join('; ')}`, 'error');
  };

  const onTreeKeyDown = (e: React.KeyboardEvent) => {
    if ((e.target as HTMLElement).tagName === 'INPUT') return;
    if ((e.key === 'Delete' || (e.key === 'Backspace' && e.metaKey)) && selection.size) {
      e.preventDefault();
      void deleteMany([...selection]);
    } else if (e.key === 'Escape') {
      setSelection(new Set());
    } else if (e.key.toLowerCase() === 'a' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      setSelection(new Set(visibleOrder));
    }
  };

  const onDropOn = async (e: React.DragEvent, target: TreeNode | null) => {
    e.preventDefault();
    e.stopPropagation();
    setDropId(null);
    const raw = e.dataTransfer.getData(NODE_MIME);
    if (!raw) return;
    const src = JSON.parse(raw) as { type: 'folder' | 'connection'; id: string };
    if (target && src.id === target.id) return;
    if (src.type === 'connection') {
      const body = !target ? {} : target.type === 'folder' ? { folder_id: target.id } : { parent_id: target.id };
      await run(() => post(`/api/v1/connections/${src.id}/move`, body));
    } else {
      if (target?.type === 'connection') return;
      await run(() => patch(`/api/v1/folders/${src.id}`, { parent_id: target ? target.id : null }));
    }
  };

  const canCreateConn = hasPerm(me, 'connections.create');
  const canCreateFolder = hasPerm(me, 'folders.create');

  const renderNode = (n: TreeNode, depth: number): React.ReactNode => {
    const isOpen = query ? true : expanded.has(n.id);
    const hasChildren = n.children.length > 0;
    const editable = n.type === 'folder' ? ['edit', 'manage'].includes(n.folder.permission ?? '')
      : ['edit', 'manage'].includes(n.conn.permission ?? '');
    const manage = n.type === 'folder' ? n.folder.permission === 'manage' : n.conn.permission === 'manage';
    const onContext = (e: React.MouseEvent) => {
      e.preventDefault();
      e.stopPropagation();
      const key = keyOf(n);
      if (selection.has(key) && selection.size > 1) {
        dialogs.contextMenu(e.clientX, e.clientY, [
          { label: `Delete ${selection.size} selected items…`, danger: true, action: () => void deleteMany([...selection]) },
          { label: 'Clear selection', action: () => setSelection(new Set()) },
        ]);
        return;
      }
      setSelection(new Set([key]));
      setAnchor(key);
      if (n.type === 'folder') {
        dialogs.contextMenu(e.clientX, e.clientY, [
          { label: 'New connection here', disabled: !editable || !canCreateConn, action: () => actions.newConnection({ folderId: n.id }) },
          { label: 'New sub-folder', disabled: !editable || !canCreateFolder, action: () => actions.newFolder(n.id) },
          { separator: true, label: '' },
          { label: 'Edit folder…', disabled: !editable, action: () => actions.editFolder(n.folder) },
          { label: 'Permissions…', disabled: !manage, action: () => actions.permissions('folder', n.id, n.folder.name) },
          { separator: true, label: '' },
          { label: 'Delete', danger: true, disabled: !manage, action: () => void deleteFolder(n.folder, hasChildren) },
        ]);
      } else {
        const c = n.conn;
        const launchable = c.protocol !== 'host';
        const canConnect = ['connect', 'edit', 'manage'].includes(c.permission ?? '');
        dialogs.contextMenu(e.clientX, e.clientY, [
          ...(launchable ? [
            { label: 'Open', disabled: !canConnect, action: () => open(c) },
            { label: 'Open in floating window', disabled: !canConnect, action: () => open(c, 'float') },
            { label: 'Open in split view', disabled: !canConnect || !focusedSession, action: () => open(c, 'split') },
            { separator: true, label: '' },
          ] : []),
          { label: 'Edit…', disabled: !editable, action: () => actions.editConnection(c) },
          { label: 'Duplicate', disabled: !editable, action: () => void run(() => post(`/api/v1/connections/${c.id}/duplicate`, { include_children: hasChildren }), 'Duplicated') },
          { label: 'New child connection', disabled: !editable || !canCreateConn, action: () => actions.newConnection({ parentId: c.id }) },
          { label: c.favorite ? 'Remove from favorites' : 'Add to favorites',
            action: () => void run(() => (c.favorite ? del(`/api/v1/connections/${c.id}/favorite`) : put(`/api/v1/connections/${c.id}/favorite`))) },
          { label: 'Permissions…', disabled: !manage, action: () => actions.permissions('connection', c.id, c.name) },
          { separator: true, label: '' },
          { label: 'Delete', danger: true, disabled: !manage, action: () => void deleteConnection(c, hasChildren) },
        ]);
      }
    };
    return (
      <li key={n.id} role="treeitem" aria-expanded={hasChildren ? isOpen : undefined}>
        <div
          className={`tree-row ${dropId === n.id ? 'drop' : ''} ${selection.has(keyOf(n)) ? 'selected' : ''}`}
          aria-selected={selection.has(keyOf(n))}
          style={{ paddingLeft: 6 + depth * 14 }}
          draggable
          onDragStart={(e) => {
            e.dataTransfer.setData(NODE_MIME, JSON.stringify({ type: n.type, id: n.id }));
            if (n.type === 'connection') e.dataTransfer.setData(CONNECTION_MIME, JSON.stringify({ id: n.conn.id, name: n.conn.name, protocol: n.conn.protocol }));
            e.dataTransfer.effectAllowed = 'move';
          }}
          onDragOver={(e) => {
            if (!e.dataTransfer.types.includes(NODE_MIME)) return;
            e.preventDefault();
            e.stopPropagation();
            setDropId(n.id);
          }}
          onDragLeave={() => setDropId((d) => (d === n.id ? null : d))}
          onDrop={(e) => void onDropOn(e, n)}
          onClick={(e) => onRowClick(e, n)}
          onDoubleClick={(e) => !e.ctrlKey && !e.metaKey && !e.shiftKey && n.type === 'connection' && open(n.conn)}
          onContextMenu={onContext}
          title={n.type === 'connection' ? undefined : n.folder.description ?? n.folder.name}
        >
          <span className="twisty" onClick={(e) => { e.stopPropagation(); if (hasChildren) toggle(n.id); }}>
            {hasChildren ? (isOpen ? <ChevronDown size={13} /> : <ChevronRight size={13} />) : null}
          </span>
          {n.type === 'folder' ? (
            <>
              <FolderGlyph icon={n.folder.icon} color={n.folder.color} open={isOpen} />
              <span className="tree-name">{n.folder.name}</span>
            </>
          ) : (
            <>
              <ProtocolIcon protocol={n.conn.protocol} />
              {renderLabel(n.conn)}
              <span className="spacer" />
              {n.conn.warnings.length > 0 && (
                <span title={n.conn.warnings.map((w) => w.message).join('\n')}><ShieldAlert size={12} className="warn-icon" /></span>
              )}
              {n.conn.favorite && <Star size={11} className="fav-icon" />}
            </>
          )}
        </div>
        {hasChildren && isOpen && <ul role="group">{n.children.map((c) => renderNode(c, depth + 1))}</ul>}
      </li>
    );
  };

  return (
    <div className="tree-panel">
      <div className="tree-tools">
        <div className="search">
          <Search size={14} />
          <input placeholder="Search name, host, tag…" value={query} onChange={(e) => setQuery(e.target.value)} />
          {query && <button className="icon-btn" onClick={() => setQuery('')} aria-label="Clear"><X size={12} /></button>}
        </div>
        <button className="icon-btn" title="View options" onMouseDown={(e) => e.stopPropagation()} onClick={viewMenu}><SlidersHorizontal size={16} /></button>
        <button className="icon-btn" title="New connection" disabled={!canCreateConn} onClick={() => actions.newConnection({})}><Plus size={16} /></button>
        <button className="icon-btn" title="New folder" disabled={!canCreateFolder} onClick={() => actions.newFolder(null)}><FolderPlus size={16} /></button>
      </div>
      {favorites.length > 0 && !query && (
        <div className="tree-section">
          <div className="section-title"><Star size={12} /> Favorites</div>
          <ul className="tree" role="tree">
            {favorites.map((c) => (
              <li key={`fav-${c.id}`}>
                <div className={`tree-row ${actions.selectedId === c.id ? 'selected' : ''}`} style={{ paddingLeft: 20 }}
                     onClick={() => actions.select(c)} onDoubleClick={() => open(c)}
                     draggable onDragStart={(e) => e.dataTransfer.setData(CONNECTION_MIME, JSON.stringify({ id: c.id, name: c.name, protocol: c.protocol }))}>
                  <ProtocolIcon protocol={c.protocol} />
                  {renderLabel(c)}
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}
      <div className={`tree-scroll ${dropId === '__root' ? 'drop-root' : ''}`}
           tabIndex={0}
           onKeyDown={onTreeKeyDown}
           onClick={(e) => { if (e.target === e.currentTarget) setSelection(new Set()); }}
           onDragOver={(e) => { if (e.dataTransfer.types.includes(NODE_MIME)) { e.preventDefault(); setDropId('__root'); } }}
           onDrop={(e) => void onDropOn(e, null)}
           onContextMenu={(e) => {
             e.preventDefault();
             dialogs.contextMenu(e.clientX, e.clientY, [
               { label: 'New connection', disabled: !canCreateConn, action: () => actions.newConnection({}) },
               { label: 'New folder', disabled: !canCreateFolder, action: () => actions.newFolder(null) },
             ]);
           }}>
        {isLoading && <div className="muted pad">Loading…</div>}
        {error && <div className="banner error">{errorMessage(error)}</div>}
        {tree && !nodes.length && <div className="muted pad">{query ? 'No matches' : 'No connections yet. Create one with “+”.'}</div>}
        {selection.size > 1 && (
          <div className="tree-selection-bar">
            <span>{selection.size} selected</span>
            <button className="danger" onClick={() => void deleteMany([...selection])}>Delete</button>
            <button onClick={() => setSelection(new Set())}>Clear</button>
          </div>
        )}
        <ul className="tree" role="tree">{nodes.map((n) => renderNode(n, 0))}</ul>
      </div>
    </div>
  );
}
