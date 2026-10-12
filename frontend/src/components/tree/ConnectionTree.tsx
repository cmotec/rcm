import { ChevronDown, ChevronRight, FolderPlus, Plus, Search, ShieldAlert, Star, X } from 'lucide-react';
import { useMemo, useState } from 'react';
import { del, errorMessage, patch, post, put } from '../../api/client';
import { hasPerm, useInvalidate, useMe, useTree } from '../../api/hooks';
import type { Connection, Folder } from '../../api/types';
import { openConnection } from '../../sessions/registry';
import { dialogs } from '../../state/dialogs';
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

  const nodes = useMemo(() => (tree ? filterTree(buildTree(tree), query) : []), [tree, query]);
  const favorites = useMemo(() => tree?.connections.filter((c) => c.favorite) ?? [], [tree]);

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
    if (mode === 'float') void openConnection(c, { floating: { x: 60, y: 40, w: 960, h: 620 } });
    else if (mode === 'split' && focusedSession) void openConnection(c, { splitWith: { sessionId: focusedSession, edge: 'right' } });
    else void openConnection(c);
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
          className={`tree-row ${dropId === n.id ? 'drop' : ''} ${n.type === 'connection' && actions.selectedId === n.id ? 'selected' : ''}`}
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
          onClick={() => (n.type === 'folder' ? toggle(n.id) : actions.select(n.conn))}
          onDoubleClick={() => n.type === 'connection' && open(n.conn)}
          onContextMenu={onContext}
          title={n.type === 'connection' ? `${n.conn.name}${n.conn.hostname ? ` — ${n.conn.hostname}` : ''}` : n.folder.description ?? n.folder.name}
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
              <span className="tree-name">{n.conn.name}</span>
              {n.conn.hostname && <span className="tree-host">{n.conn.hostname}</span>}
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
                  <span className="tree-name">{c.name}</span>
                  {c.hostname && <span className="tree-host">{c.hostname}</span>}
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}
      <div className={`tree-scroll ${dropId === '__root' ? 'drop-root' : ''}`}
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
        <ul className="tree" role="tree">{nodes.map((n) => renderNode(n, 0))}</ul>
      </div>
    </div>
  );
}
