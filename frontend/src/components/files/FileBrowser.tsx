import {
  ArrowUp,
  Download,
  Eye,
  EyeOff,
  File as FileIcon,
  Folder,
  FolderPlus,
  House,
  KeyRound,
  Link2,
  Pencil,
  RefreshCw,
  Trash,
  Upload,
  X,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ApiError, api, buildUrl, csrfHeader, errorMessage, get, post } from '../../api/client';
import type { FileEntry } from '../../api/types';
import type { FileSession } from '../../sessions/FileSession';
import { formatBytes, formatDate, saveBlob } from '../../util/download';
import { useDialogs } from '../../state/dialogs';

interface Transfer {
  id: string;
  name: string;
  direction: 'upload' | 'download';
  size: number | null;
  transferred: number;
  state: 'running' | 'done' | 'failed' | 'cancelled';
  error?: string;
  abort?: () => void;
}

type SortKey = 'name' | 'size' | 'modified';

const join = (dir: string, name: string) => (dir.endsWith('/') ? dir + name : `${dir}/${name}`);
const parentOf = (p: string) => (p === '/' ? '/' : p.replace(/\/[^/]+\/?$/, '') || '/');
const DIRECT_DOWNLOAD_THRESHOLD = 512 * 1024 * 1024;

export function FileBrowser({ session }: { session: FileSession }) {
  const base = `/api/v1/sessions/${session.id}/fs`;
  const [path, setPath] = useState<string | null>(null);
  const [pathInput, setPathInput] = useState('');
  const [entries, setEntries] = useState<FileEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [transfers, setTransfers] = useState<Transfer[]>([]);
  const [showHidden, setShowHidden] = useState(session.defaultShowHidden);
  const [sort, setSort] = useState<{ key: SortKey; asc: boolean }>({ key: 'name', asc: true });
  const [dragOver, setDragOver] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const dialogs = useDialogs();

  const handleError = useCallback(
    (err: unknown) => {
      if (err instanceof ApiError && err.code === 'session_disconnected') session.markDisconnected(err.message);
      setError(errorMessage(err));
    },
    [session],
  );

  const load = useCallback(
    async (p: string) => {
      setLoading(true);
      setError(null);
      try {
        const res = await get<{ path: string; entries: FileEntry[] }>(`${base}/list`, { path: p });
        setEntries(res.entries);
        setPath(res.path);
        setPathInput(res.path);
        setSelected(new Set());
        session.markConnected();
      } catch (err) {
        handleError(err);
      } finally {
        setLoading(false);
      }
    },
    [base, handleError, session],
  );

  useEffect(() => {
    get<{ path: string }>(`${base}/home`)
      .then((r) => load(r.path))
      .catch(handleError);
  }, [base, load, handleError]);

  const visible = useMemo(() => {
    const list = entries.filter((e) => showHidden || !e.name.startsWith('.'));
    const dir = (e: FileEntry) => (e.type === 'dir' ? 0 : 1);
    return [...list].sort((a, b) => {
      if (dir(a) !== dir(b)) return dir(a) - dir(b);
      let c = 0;
      if (sort.key === 'name') c = a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' });
      if (sort.key === 'size') c = (a.size ?? 0) - (b.size ?? 0);
      if (sort.key === 'modified') c = (a.modified ?? '').localeCompare(b.modified ?? '');
      return sort.asc ? c : -c;
    });
  }, [entries, showHidden, sort]);

  const updateTransfer = (id: string, patch: Partial<Transfer>) =>
    setTransfers((ts) => ts.map((t) => (t.id === id ? { ...t, ...patch } : t)));

  // ------------------------------------------------------------------ upload

  const uploadOne = (file: File, target: string, overwrite = false): Promise<void> =>
    new Promise((resolve) => {
      const id = crypto.randomUUID();
      const xhr = new XMLHttpRequest();
      const tr: Transfer = { id, name: target, direction: 'upload', size: file.size, transferred: 0, state: 'running',
        abort: () => xhr.abort() };
      setTransfers((ts) => [...ts.filter((t) => t.name !== target || t.state === 'running'), tr]);
      xhr.open('PUT', buildUrl(`${base}/upload`, { path: target, overwrite: overwrite ? 'true' : 'false' }));
      for (const [k, v] of Object.entries(csrfHeader())) xhr.setRequestHeader(k, v);
      xhr.setRequestHeader('Content-Type', 'application/octet-stream');
      xhr.setRequestHeader('X-Transfer-Id', id);
      xhr.upload.onprogress = (e) => updateTransfer(id, { transferred: e.loaded });
      xhr.onload = async () => {
        if (xhr.status === 201) {
          updateTransfer(id, { state: 'done', transferred: file.size });
          resolve();
          return;
        }
        let detail = xhr.statusText;
        let code = '';
        try {
          const p = JSON.parse(xhr.responseText) as { detail?: string; code?: string };
          detail = p.detail ?? detail;
          code = p.code ?? '';
        } catch {
          /* not JSON */
        }
        if (xhr.status === 409 && code === 'file_exists' && !overwrite) {
          setTransfers((ts) => ts.filter((t) => t.id !== id));
          const ok = await dialogs.confirm({ title: 'File exists', message: `Overwrite ${target}?`, confirmLabel: 'Overwrite' });
          if (ok) await uploadOne(file, target, true);
          resolve();
          return;
        }
        updateTransfer(id, { state: 'failed', error: detail });
        resolve();
      };
      xhr.onerror = () => {
        updateTransfer(id, { state: 'failed', error: 'network error' });
        resolve();
      };
      xhr.onabort = () => {
        updateTransfer(id, { state: 'cancelled' });
        resolve();
      };
      xhr.send(file);
    });

  const uploadFiles = async (files: { file: File; rel: string }[], dir: string) => {
    const madeDirs = new Set<string>();
    for (const { file, rel } of files) {
      const parts = rel.split('/').slice(0, -1);
      let cur = dir;
      for (const p of parts) {
        cur = join(cur, p);
        if (!madeDirs.has(cur)) {
          madeDirs.add(cur);
          await post(`${base}/mkdir`, { path: cur }).catch(() => undefined);
        }
      }
      await uploadOne(file, join(dir, rel));
    }
    if (path === dir) await load(dir);
  };

  const onDrop = async (e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    if (!path) return;
    const items = Array.from(e.dataTransfer.items ?? []);
    const collected: { file: File; rel: string }[] = [];
    const walk = async (entry: FileSystemEntry, prefix: string): Promise<void> => {
      if (entry.isFile) {
        const file = await new Promise<File>((res, rej) => (entry as FileSystemFileEntry).file(res, rej));
        collected.push({ file, rel: prefix + file.name });
      } else if (entry.isDirectory) {
        const reader = (entry as FileSystemDirectoryEntry).createReader();
        let batch: FileSystemEntry[];
        do {
          batch = await new Promise<FileSystemEntry[]>((res, rej) => reader.readEntries(res, rej));
          for (const child of batch) await walk(child, `${prefix}${entry.name}/`);
        } while (batch.length);
      }
    };
    const entriesDropped = items.map((i) => i.webkitGetAsEntry?.()).filter((x): x is FileSystemEntry => Boolean(x));
    if (entriesDropped.length) {
      for (const en of entriesDropped) await walk(en, '');
    } else {
      Array.from(e.dataTransfer.files).forEach((f) => collected.push({ file: f, rel: f.name }));
    }
    await uploadFiles(collected, path);
  };

  // ------------------------------------------------------------------ download

  const download = async (entry: FileEntry) => {
    const url = buildUrl(`${base}/download`, { path: entry.path });
    if ((entry.size ?? 0) > DIRECT_DOWNLOAD_THRESHOLD) {
      // very large: let the browser stream it to disk (progress shown by the browser)
      const a = document.createElement('a');
      a.href = url;
      a.download = entry.name;
      a.click();
      return;
    }
    const id = crypto.randomUUID();
    const ctrl = new AbortController();
    setTransfers((ts) => [...ts, { id, name: entry.path, direction: 'download', size: entry.size, transferred: 0,
      state: 'running', abort: () => ctrl.abort() }]);
    try {
      const resp = await api<Response>(url, { raw: true, signal: ctrl.signal });
      const reader = resp.body!.getReader();
      const chunks: BlobPart[] = [];
      let received = 0;
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        chunks.push(value);
        received += value.length;
        updateTransfer(id, { transferred: received });
      }
      saveBlob(new Blob(chunks), entry.name);
      updateTransfer(id, { state: 'done' });
    } catch (err) {
      updateTransfer(id, ctrl.signal.aborted ? { state: 'cancelled' } : { state: 'failed', error: errorMessage(err) });
    }
  };

  // ------------------------------------------------------------------ actions

  const selectedEntries = visible.filter((e) => selected.has(e.path));

  const act = async (fn: () => Promise<unknown>) => {
    try {
      await fn();
      if (path) await load(path);
    } catch (err) {
      handleError(err);
    }
  };

  const mkdir = async () => {
    const name = await dialogs.prompt({ title: 'New folder', label: 'Folder name' });
    if (name && path) await act(() => post(`${base}/mkdir`, { path: join(path, name) }));
  };

  const rename = async (entry: FileEntry) => {
    const target = await dialogs.prompt({ title: 'Rename / move', label: 'New name or absolute path', initial: entry.name });
    if (!target || !path || target === entry.name) return;
    await act(() => post(`${base}/rename`, { source: entry.path, target: target.startsWith('/') ? target : join(path, target) }));
  };

  const remove = async (list: FileEntry[]) => {
    if (!list.length) return;
    const dirs = list.some((e) => e.type === 'dir');
    const ok = await dialogs.confirm({
      title: 'Delete',
      message: `Delete ${list.length === 1 ? list[0].name : `${list.length} items`}${dirs ? ' (directories recursively)' : ''}?`,
      confirmLabel: 'Delete',
      danger: true,
    });
    if (!ok) return;
    await act(async () => {
      for (const e of list) await post(`${base}/delete`, { path: e.path, recursive: e.type === 'dir' });
    });
  };

  const chmod = async (entry: FileEntry) => {
    const value = await dialogs.prompt({ title: `Permissions of ${entry.name}`, label: 'Octal mode (e.g. 644)',
      initial: entry.mode !== null ? entry.mode.toString(8) : '644' });
    if (!value) return;
    const mode = Number.parseInt(value, 8);
    if (Number.isNaN(mode)) return;
    await act(() => post(`${base}/chmod`, { path: entry.path, mode }));
  };

  const toggleSelect = (e: FileEntry, ev: React.MouseEvent) => {
    setSelected((s) => {
      const next = new Set(ev.ctrlKey || ev.metaKey ? s : []);
      if (next.has(e.path) && (ev.ctrlKey || ev.metaKey)) next.delete(e.path);
      else next.add(e.path);
      return next;
    });
  };

  const sortBy = (key: SortKey) => setSort((s) => ({ key, asc: s.key === key ? !s.asc : true }));
  const running = transfers.filter((t) => t.state === 'running').length;
  const unencrypted = session.info.protocol === 'ftp';

  return (
    <div className="fb" onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
         onDragLeave={() => setDragOver(false)} onDrop={onDrop}>
      <div className="fb-toolbar">
        <button className="icon-btn" title="Parent directory" disabled={!path || path === '/'}
                onClick={() => path && load(parentOf(path))}><ArrowUp size={16} /></button>
        <button className="icon-btn" title="Home" onClick={() => get<{ path: string }>(`${base}/home`).then((r) => load(r.path)).catch(handleError)}>
          <House size={16} />
        </button>
        <button className="icon-btn" title="Refresh" onClick={() => path && load(path)}>
          <RefreshCw size={16} className={loading ? 'spin' : ''} />
        </button>
        <form className="fb-path" onSubmit={(e) => { e.preventDefault(); void load(pathInput || '/'); }}>
          <input value={pathInput} onChange={(e) => setPathInput(e.target.value)} spellCheck={false} aria-label="Path" />
        </form>
        <button className="icon-btn" title="New folder" onClick={mkdir}><FolderPlus size={16} /></button>
        <button className="icon-btn" title="Upload files" onClick={() => fileInput.current?.click()}><Upload size={16} /></button>
        <button className="icon-btn" title="Download selected" disabled={!selectedEntries.some((e) => e.type === 'file')}
                onClick={() => selectedEntries.filter((e) => e.type === 'file').forEach((e) => void download(e))}>
          <Download size={16} />
        </button>
        <button className="icon-btn" title="Rename" disabled={selectedEntries.length !== 1}
                onClick={() => rename(selectedEntries[0])}><Pencil size={16} /></button>
        {session.supportsChmod && (
          <button className="icon-btn" title="Permissions" disabled={selectedEntries.length !== 1}
                  onClick={() => chmod(selectedEntries[0])}><KeyRound size={16} /></button>
        )}
        <button className="icon-btn danger" title="Delete" disabled={!selectedEntries.length}
                onClick={() => remove(selectedEntries)}><Trash size={16} /></button>
        <button className="icon-btn" title={showHidden ? 'Hide hidden files' : 'Show hidden files'}
                onClick={() => setShowHidden((v) => !v)}>{showHidden ? <EyeOff size={16} /> : <Eye size={16} />}</button>
        <input ref={fileInput} type="file" multiple hidden onChange={(e) => {
          const files = Array.from(e.target.files ?? []);
          e.target.value = '';
          if (path) void uploadFiles(files.map((f) => ({ file: f, rel: f.name })), path);
        }} />
      </div>
      {unencrypted && <div className="banner warn">Plain FTP: credentials and files are transferred unencrypted.</div>}
      {error && (
        <div className="banner error">
          {error}
          {session.state.status === 'disconnected' && (
            <button className="link-btn" onClick={() => session.reconnect().then(() => { if (path) void load(path); })}>Reconnect</button>
          )}
          <button className="icon-btn" onClick={() => setError(null)} aria-label="Dismiss"><X size={14} /></button>
        </div>
      )}
      <div className={`fb-table-wrap ${dragOver ? 'drag-over' : ''}`}>
        <table className="fb-table">
          <thead>
            <tr>
              <th onClick={() => sortBy('name')}>Name</th>
              <th className="num" onClick={() => sortBy('size')}>Size</th>
              <th onClick={() => sortBy('modified')}>Modified</th>
              <th>Permissions</th>
              <th>Owner</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((e) => (
              <tr key={e.path} className={selected.has(e.path) ? 'selected' : ''}
                  onClick={(ev) => toggleSelect(e, ev)}
                  onDoubleClick={() => (e.type === 'dir' ? load(e.path) : download(e))}
                  draggable={e.type === 'file'}
                  onContextMenu={(ev) => {
                    ev.preventDefault();
                    setSelected(new Set([e.path]));
                    dialogs.contextMenu(ev.clientX, ev.clientY, [
                      ...(e.type === 'dir' ? [{ label: 'Open', action: () => void load(e.path) }]
                        : [{ label: 'Download', action: () => void download(e) }]),
                      { label: 'Rename / move…', action: () => void rename(e) },
                      ...(session.supportsChmod ? [{ label: 'Permissions…', action: () => void chmod(e) }] : []),
                      { label: 'Delete', danger: true, action: () => void remove([e]) },
                    ]);
                  }}>
                <td className="name">
                  {e.type === 'dir' ? <Folder size={15} className="folder-icon" /> : e.type === 'link'
                    ? <Link2 size={15} /> : <FileIcon size={15} />}
                  <span>{e.name}</span>
                  {e.link_target && <span className="muted"> → {e.link_target}</span>}
                </td>
                <td className="num">{e.type === 'dir' ? '' : formatBytes(e.size)}</td>
                <td>{formatDate(e.modified)}</td>
                <td className="mono">{e.permissions ?? ''}</td>
                <td>{[e.owner, e.group].filter(Boolean).join(':')}</td>
              </tr>
            ))}
            {!visible.length && !loading && (
              <tr><td colSpan={5} className="muted center">Empty directory — drop files here to upload</td></tr>
            )}
          </tbody>
        </table>
      </div>
      {transfers.length > 0 && (
        <div className="fb-transfers">
          <div className="fb-transfers-head">
            <span>Transfers {running ? `(${running} running)` : ''}</span>
            <button className="link-btn" onClick={() => setTransfers((ts) => ts.filter((t) => t.state === 'running'))}>
              Clear finished
            </button>
          </div>
          {transfers.map((t) => {
            const pct = t.size ? Math.min(100, Math.round((t.transferred / t.size) * 100)) : null;
            return (
              <div key={t.id} className={`transfer ${t.state}`}>
                {t.direction === 'upload' ? <Upload size={13} /> : <Download size={13} />}
                <span className="transfer-name" title={t.name}>{t.name}</span>
                <progress max={100} value={t.state === 'done' ? 100 : pct ?? undefined} />
                <span className="transfer-meta">
                  {t.state === 'failed' ? t.error : t.state === 'cancelled' ? 'cancelled'
                    : `${formatBytes(t.transferred)}${t.size ? ` / ${formatBytes(t.size)}` : ''}`}
                </span>
                {t.state === 'running' && t.abort && (
                  <button className="icon-btn" title="Cancel" onClick={t.abort}><X size={13} /></button>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
