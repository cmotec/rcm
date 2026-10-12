import type { Connection, Folder, Tree } from '../../api/types';

export type TreeNode =
  | { type: 'folder'; id: string; folder: Folder; children: TreeNode[] }
  | { type: 'connection'; id: string; conn: Connection; children: TreeNode[] };

const byOrder = (a: { sort_order: number; name: string }, b: { sort_order: number; name: string }) =>
  a.sort_order - b.sort_order || a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' });

export function buildTree(tree: Tree): TreeNode[] {
  const folderIds = new Set(tree.folders.map((f) => f.id));
  const connIds = new Set(tree.connections.map((c) => c.id));
  const folderChildren = new Map<string | null, Folder[]>();
  const connsInFolder = new Map<string | null, Connection[]>();
  const connChildren = new Map<string, Connection[]>();
  for (const f of tree.folders) {
    const key = f.parent_id && folderIds.has(f.parent_id) ? f.parent_id : null;
    folderChildren.set(key, [...(folderChildren.get(key) ?? []), f]);
  }
  for (const c of tree.connections) {
    if (c.parent_id && connIds.has(c.parent_id)) {
      connChildren.set(c.parent_id, [...(connChildren.get(c.parent_id) ?? []), c]);
    } else {
      const key = c.folder_id && folderIds.has(c.folder_id) ? c.folder_id : null;
      connsInFolder.set(key, [...(connsInFolder.get(key) ?? []), c]);
    }
  }
  const connNode = (c: Connection): TreeNode => ({
    type: 'connection',
    id: c.id,
    conn: c,
    children: (connChildren.get(c.id) ?? []).sort(byOrder).map(connNode),
  });
  const folderLevel = (parent: string | null): TreeNode[] => [
    ...(folderChildren.get(parent) ?? []).sort(byOrder).map((f): TreeNode => ({
      type: 'folder',
      id: f.id,
      folder: f,
      children: folderLevel(f.id),
    })),
    ...(connsInFolder.get(parent) ?? []).sort(byOrder).map(connNode),
  ];
  return folderLevel(null);
}

export function matches(c: Connection, q: string): boolean {
  const s = q.toLowerCase();
  return (
    c.name.toLowerCase().includes(s) ||
    (c.hostname ?? '').toLowerCase().includes(s) ||
    (c.description ?? '').toLowerCase().includes(s) ||
    c.tags.some((t) => t.toLowerCase().includes(s)) ||
    c.protocol === s
  );
}

/** Keep nodes matching the query plus their ancestors. */
export function filterTree(nodes: TreeNode[], q: string): TreeNode[] {
  if (!q.trim()) return nodes;
  const out: TreeNode[] = [];
  for (const n of nodes) {
    const children = filterTree(n.children, q);
    const self = n.type === 'connection' ? matches(n.conn, q) : n.folder.name.toLowerCase().includes(q.toLowerCase());
    if (self || children.length) out.push({ ...n, children: self && n.type === 'folder' ? n.children : children } as TreeNode);
  }
  return out;
}
