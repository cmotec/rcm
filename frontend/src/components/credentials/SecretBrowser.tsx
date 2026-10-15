import { ChevronRight, CornerLeftUp, FileKey, Folder, KeyRound, LoaderCircle } from 'lucide-react';
import { useEffect, useState } from 'react';
import { errorMessage, get } from '../../api/client';
import type { SecretProvider } from '../../api/types';
import { Modal } from '../common/Modal';

interface BrowseItem {
  path: string;
  key: string | null;
  is_folder: boolean;
}

/** Shown for a key whose name is empty (OpenBao allows that). */
export const UNNAMED_KEY = '(no name)';

function parentOf(path: string): string {
  const parts = path.split('/').filter(Boolean);
  parts.pop();
  return parts.join('/');
}

/**
 * Pick a secret from a provider: folders → secrets → keys. Only names are listed, never values
 * (needs the "secrets.manage" permission).
 */
export function SecretBrowser({ provider, initialPath, onPick, onClose }: {
  provider: SecretProvider;
  initialPath?: string;
  onPick: (path: string, key: string) => void;
  onClose: () => void;
}) {
  const [path, setPath] = useState((initialPath ?? '').replace(/^\/+|\/+$/g, ''));
  const [items, setItems] = useState<BrowseItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setItems(null);
    setError(null);
    get<BrowseItem[]>(`/api/v1/secret-providers/${provider.id}/browse`, { path: path || '/' })
      .then((r) => !cancelled && setItems(r))
      .catch((e) => !cancelled && setError(errorMessage(e)));
    return () => {
      cancelled = true;
    };
  }, [provider.id, path]);

  const folders = (items ?? []).filter((i) => i.key === null && i.is_folder);
  const secrets = (items ?? []).filter((i) => i.key === null && !i.is_folder);
  const keys = (items ?? []).filter((i) => i.key !== null);
  const mount = typeof provider.config.mount === 'string' ? provider.config.mount : null;

  return (
    <Modal title={`Browse ${provider.name}`} onClose={onClose} width={560} footer={<button onClick={onClose}>Cancel</button>}>
      <div className="secret-path">
        <span className="muted">{mount ? `${mount} /` : '/'}</span> <b className="mono">{path || '(top level)'}</b>
        {path && <button className="icon-btn" title="Up" onClick={() => setPath(parentOf(path))}><CornerLeftUp size={14} /></button>}
      </div>
      {error && <p className="banner error">{error}</p>}
      {!items && !error && <p className="muted"><LoaderCircle className="spin" size={14} /> Loading…</p>}
      {items && !items.length && <p className="muted">Nothing here.</p>}
      <ul className="choice-list secret-list">
        {folders.map((i) => (
          <li key={`f:${i.path}`} onClick={() => setPath(i.path)}>
            <Folder size={14} /> <span>{i.path.split('/').pop()}/</span> <ChevronRight size={14} className="muted" />
          </li>
        ))}
        {secrets.map((i) => (
          <li key={`s:${i.path}`} onClick={() => setPath(i.path)}>
            <FileKey size={14} /> <span>{i.path.split('/').pop()}</span> <ChevronRight size={14} className="muted" />
          </li>
        ))}
        {keys.map((i) => (
          <li key={`k:${i.key}`} onClick={() => onPick(i.path, i.key ?? '')} title="Use this key">
            <KeyRound size={14} /> <span className={i.key ? 'mono' : 'muted'}>{i.key || UNNAMED_KEY}</span>
          </li>
        ))}
      </ul>
      {keys.length > 0 && <p className="muted small">Choose the key that holds the value. Values are never shown here.</p>}
    </Modal>
  );
}
