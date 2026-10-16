import { Cloud, Eye, Folder, FolderPlus, KeyRound, Layers, Pencil, Plus, Search, Shield, Trash, TriangleAlert } from 'lucide-react';
import { useEffect, useState } from 'react';
import { del, errorMessage, get, patch, post, put } from '../../api/client';
import { folderPath, folderSubtree, hasPerm, useCredentialFolders, useCredentials, useInvalidate, useMe } from '../../api/hooks';
import type { Credential, CredentialFolder, CredentialSecrets, SecretMapping, SecretProvider } from '../../api/types';
import { dialogs } from '../../state/dialogs';
import { formatDate } from '../../util/download';
import { AclDialog } from '../dialogs/AclDialog';
import { Modal } from '../common/Modal';
import { MOBILE_QUERY, useMediaQuery } from '../../util/useMediaQuery';
import { SecretBrowser } from './SecretBrowser';

type SecretField = 'password' | 'private_key' | 'passphrase';

function SecretInput({ label, isSet, value, onChange, multiline }: {
  label: string; isSet: boolean; value: string | null; onChange: (v: string | null) => void; multiline?: boolean;
}) {
  if (value === null && isSet) {
    return (
      <div className="field"><span>{label}</span>
        <div className="row">
          <span className="muted">•••••••• stored (encrypted)</span>
          <button type="button" onClick={() => onChange('')}>Replace</button>
          <button type="button" className="danger" onClick={() => onChange('')} title="Save with an empty value to remove">Remove</button>
        </div>
      </div>
    );
  }
  return (
    <label className="field"><span>{label}</span>
      {multiline ? <textarea rows={5} className="mono" value={value ?? ''} onChange={(e) => onChange(e.target.value)} spellCheck={false}
                             placeholder={isSet ? 'empty = remove stored value' : '-----BEGIN OPENSSH PRIVATE KEY-----'} />
        : <input type="password" value={value ?? ''} onChange={(e) => onChange(e.target.value)} autoComplete="new-password"
                 placeholder={isSet ? 'empty = remove stored value' : 'optional'} />}
    </label>
  );
}

/** Credential folder picker (shows the full path). */
export function CredentialFolderSelect({ folders, value, onChange, emptyLabel = '— no folder —', disabled }: {
  folders: CredentialFolder[]; value: string; onChange: (id: string) => void; emptyLabel?: string; disabled?: boolean;
}) {
  const sorted = [...folders].sort((a, b) => folderPath(folders, a.id).localeCompare(folderPath(folders, b.id)));
  return (
    <select value={value} disabled={disabled} onChange={(e) => onChange(e.target.value)}>
      <option value="">{emptyLabel}</option>
      {sorted.map((f) => <option key={f.id} value={f.id}>{folderPath(folders, f.id)}</option>)}
    </select>
  );
}

function CredentialDialog({ credential, all, folders, defaultFolder, onClose }: {
  credential?: Credential; all: Credential[]; folders: CredentialFolder[]; defaultFolder: string | null; onClose: () => void;
}) {
  const invalidate = useInvalidate();
  const [name, setName] = useState(credential?.name ?? '');
  const [folderId, setFolderId] = useState(credential ? credential.folder_id ?? '' : defaultFolder ?? '');
  const [attributes, setAttributes] = useState<[string, string][]>(Object.entries(credential?.attributes ?? {}));
  const [username, setUsername] = useState(credential?.username ?? '');
  const [domain, setDomain] = useState(credential?.domain ?? '');
  const [parentId, setParentId] = useState(credential?.parent_id ?? '');
  const [description, setDescription] = useState(credential?.description ?? '');
  const [rotation, setRotation] = useState(credential?.rotation_interval_days ? String(credential.rotation_interval_days) : '');
  const [secrets, setSecrets] = useState<Record<SecretField, string | null>>({ password: null, private_key: null, passphrase: null });
  const [error, setError] = useState<string | null>(null);

  const save = async () => {
    const body: Record<string, unknown> = {
      name: name.trim(),
      username: username.trim() || null,
      domain: domain.trim() || null,
      parent_id: parentId || null,
      description: description.trim() || null,
      rotation_interval_days: rotation ? Number(rotation) : null,
      folder_id: folderId || null,
      attributes: Object.fromEntries(attributes.filter(([k]) => k.trim()).map(([k, v]) => [k.trim(), v])),
    };
    for (const f of ['password', 'private_key', 'passphrase'] as SecretField[]) {
      if (secrets[f] !== null) body[f] = secrets[f] || null;
    }
    try {
      if (credential) await patch(`/api/v1/credentials/${credential.id}`, { ...body, version: credential.version });
      else await post('/api/v1/credentials', body);
      invalidate.credentials();
      dialogs.toast('Credential saved', 'success');
      onClose();
    } catch (err) {
      setError(errorMessage(err));
    }
  };

  return (
    <Modal title={credential ? `Edit ${credential.name}` : 'New credential'} onClose={onClose} width={600}
           footer={<>
             {error && <span className="form-error">{error}</span>}
             <button onClick={onClose}>Cancel</button>
             <button className="primary" disabled={!name.trim()} onClick={save}>Save</button>
           </>}>
      <p className="muted">Every field is optional. Empty fields are inherited from the parent credential (if any).</p>
      {credential?.external_fields.length ? (
        <div className="banner info"><Cloud size={14} /> Synchronised from an external secret manager: {credential.external_fields.join(', ')}.
          Local edits to these fields are reported as conflicts at the next sync.</div>
      ) : null}
      <div className="form-grid">
        <label className="field span2"><span>Name</span><input value={name} onChange={(e) => setName(e.target.value)} autoFocus /></label>
        <label className="field"><span>Username</span><input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="off" /></label>
        <label className="field"><span>Domain</span><input value={domain} onChange={(e) => setDomain(e.target.value)} /></label>
        <div className="span2"><SecretInput label="Password" isSet={Boolean(credential?.password_set)} value={secrets.password}
                                            onChange={(v) => setSecrets({ ...secrets, password: v })} /></div>
        <div className="span2"><SecretInput label="SSH private key" multiline isSet={Boolean(credential?.private_key_set)} value={secrets.private_key}
                                            onChange={(v) => setSecrets({ ...secrets, private_key: v })} /></div>
        {credential?.private_key_fingerprint && <div className="span2 muted mono">Key fingerprint: {credential.private_key_fingerprint}</div>}
        <div className="span2"><SecretInput label="Private key passphrase" isSet={Boolean(credential?.passphrase_set)} value={secrets.passphrase}
                                            onChange={(v) => setSecrets({ ...secrets, passphrase: v })} /></div>
        <label className="field"><span>Inherit missing fields from</span>
          <select value={parentId} onChange={(e) => setParentId(e.target.value)}>
            <option value="">— none —</option>
            {all.filter((c) => c.id !== credential?.id).map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
        </label>
        <label className="field"><span>Rotation reminder (days)</span>
          <input type="number" min={1} value={rotation} onChange={(e) => setRotation(e.target.value)} placeholder="off" />
        </label>
        <label className="field span2"><span>Description</span><textarea rows={2} value={description} onChange={(e) => setDescription(e.target.value)} /></label>
        <label className="field span2"><span>Folder</span>
          <CredentialFolderSelect folders={folders} value={folderId} onChange={setFolderId} />
        </label>
        <div className="field span2">
          <span>Attributes <small className="muted">(not secret, stored unencrypted; connections can read them with
            {' '}<code>{'{{lookup:<name>:<attribute>}}'}</code>, e.g. port, hostname, description)</small></span>
          {attributes.map(([k, v], i) => (
            <div key={i} className="row nowrap attr-row">
              <input value={k} placeholder="name, e.g. port" onChange={(e) => setAttributes(attributes.map((a, j) => (j === i ? [e.target.value, a[1]] : a)))} />
              <input value={v} placeholder="value" onChange={(e) => setAttributes(attributes.map((a, j) => (j === i ? [a[0], e.target.value] : a)))} />
              <button type="button" className="icon-btn danger" title="Remove" onClick={() => setAttributes(attributes.filter((_, j) => j !== i))}><Trash size={14} /></button>
            </div>
          ))}
          <div><button type="button" onClick={() => setAttributes([...attributes, ['', '']])}><Plus size={14} /> Add attribute</button></div>
        </div>
      </div>
    </Modal>
  );
}

function RevealDialog({ credential, onClose }: { credential: Credential; onClose: () => void }) {
  const [reason, setReason] = useState('');
  const [values, setValues] = useState<CredentialSecrets | null>(null);
  const [error, setError] = useState<string | null>(null);
  const reveal = async () => {
    try {
      const fields = [credential.password_set && 'password', credential.private_key_set && 'private_key',
        credential.passphrase_set && 'passphrase'].filter(Boolean);
      setValues(await post<CredentialSecrets>(`/api/v1/credentials/${credential.id}/reveal`, { reason: reason || null, fields }));
    } catch (err) {
      setError(errorMessage(err));
    }
  };
  return (
    <Modal title={`Reveal secrets — ${credential.name}`} onClose={onClose} width={560}
           footer={values ? <button className="primary" onClick={onClose}>Done</button> : <>
             {error && <span className="form-error">{error}</span>}
             <button onClick={onClose}>Cancel</button>
             <button className="primary danger" onClick={reveal}>Reveal</button>
           </>}>
      {!values ? (
        <>
          <p><TriangleAlert size={14} /> Revealing secrets is recorded in the audit log.</p>
          <label className="field"><span>Reason (optional, stored in the audit log)</span>
            <input value={reason} onChange={(e) => setReason(e.target.value)} autoFocus />
          </label>
        </>
      ) : (
        <dl className="kv reveal">
          <dt>Username</dt><dd className="mono">{values.username ?? '—'}</dd>
          <dt>Domain</dt><dd className="mono">{values.domain ?? '—'}</dd>
          <dt>Password</dt><dd><input readOnly className="mono" value={values.password ?? ''} onFocus={(e) => e.currentTarget.select()} /></dd>
          {values.private_key && <><dt>Private key</dt><dd><textarea readOnly rows={6} className="mono" value={values.private_key} /></dd></>}
          {values.passphrase && <><dt>Passphrase</dt><dd><input readOnly className="mono" value={values.passphrase} /></dd></>}
        </dl>
      )}
    </Modal>
  );
}

function MappingsDialog({ credential, onClose }: { credential: Credential; onClose: () => void }) {
  const invalidate = useInvalidate();
  const [providers, setProviders] = useState<SecretProvider[]>([]);
  const [mappings, setMappings] = useState<SecretMapping[]>([]);
  const [draft, setDraft] = useState<{ field: string; provider_id: string; path: string; key: string; conflict_policy: string; delete_policy: string }[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [browsing, setBrowsing] = useState<number | null>(null);

  const load = async () => {
    const [p, m] = await Promise.all([
      get<SecretProvider[]>('/api/v1/secret-providers'),
      get<SecretMapping[]>(`/api/v1/credentials/${credential.id}/mappings`),
    ]);
    setProviders(p);
    setMappings(m);
    setDraft(m.map((x) => ({ field: x.field, provider_id: x.provider_id, path: x.external_ref.path, key: x.external_ref.key,
      conflict_policy: x.conflict_policy, delete_policy: x.delete_policy })));
  };
  useEffect(() => { load().catch((e) => setError(errorMessage(e))); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const save = async () => {
    try {
      await put(`/api/v1/credentials/${credential.id}/mappings`, draft.map((d) => ({
        field: d.field, provider_id: d.provider_id, external_ref: { path: d.path || '/', key: d.key },
        conflict_policy: d.conflict_policy, delete_policy: d.delete_policy,
      })));
      for (const pid of new Set(draft.map((d) => d.provider_id))) {
        await post(`/api/v1/secret-providers/${pid}/sync`, undefined, { credential_id: credential.id });
      }
      await load();
      invalidate.credentials();
      dialogs.toast('Mappings saved and synchronised', 'success');
    } catch (err) {
      setError(errorMessage(err));
    }
  };

  const resolve = async (m: SecretMapping, action: 'accept_remote' | 'keep_local') => {
    try {
      await post(`/api/v1/secret-mappings/${m.id}/resolve`, { action });
      await load();
    } catch (err) {
      setError(errorMessage(err));
    }
  };

  return (
    <Modal title={`External secrets — ${credential.name}`} onClose={onClose} width={820}
           footer={<>
             {error && <span className="form-error">{error}</span>}
             <button onClick={onClose}>Close</button>
             <button className="primary" disabled={!providers.length} onClick={save}>Save & sync</button>
           </>}>
      {!providers.length && <p className="muted">No secret providers are configured. An administrator can add OpenBao under Administration → Secret providers.</p>}
      <table className="grid stack">
        <thead><tr><th>Field</th><th>Provider</th><th>Path</th><th>Key</th><th>On conflict</th><th>On remote delete</th><th>Status</th><th /></tr></thead>
        <tbody>
          {draft.map((d, i) => {
            const m = mappings.find((x) => x.field === d.field);
            const set = (patchObj: Partial<typeof d>) => setDraft(draft.map((x, j) => (j === i ? { ...x, ...patchObj } : x)));
            return (
              <tr key={i}>
                <td data-label="Field">
                  <select value={d.field.startsWith('attr:') ? 'attr:' : d.field}
                          onChange={(e) => set({ field: e.target.value === 'attr:' ? (d.field.startsWith('attr:') ? d.field : 'attr:') : e.target.value })}>
                    {['username', 'password', 'domain', 'private_key', 'passphrase'].map((f) => <option key={f}>{f}</option>)}
                    <option value="attr:">attribute…</option>
                  </select>
                  {d.field.startsWith('attr:') && (
                    <input value={d.field.slice(5)} placeholder="e.g. port" title="Attribute name (letters, digits, _ - .)"
                           onChange={(e) => set({ field: `attr:${e.target.value.replace(/[^A-Za-z0-9_.-]/g, '')}` })} />
                  )}
                </td>
                <td data-label="Provider"><select value={d.provider_id} onChange={(e) => set({ provider_id: e.target.value })}>
                  {providers.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
                </select></td>
                <td data-label="Path">
                  <div className="row nowrap">
                    <input value={d.path} onChange={(e) => set({ path: e.target.value })} placeholder="/ or secret/path" />
                    <button className="icon-btn" title="Browse the provider" disabled={!d.provider_id} onClick={() => setBrowsing(i)}>
                      <Search size={14} />
                    </button>
                  </div>
                </td>
                <td data-label="Key"><input value={d.key} onChange={(e) => set({ key: e.target.value })} placeholder="(no name)"
                           title="Name of the key inside the secret; leave empty for a key without a name" /></td>
                <td data-label="On conflict"><select value={d.conflict_policy} onChange={(e) => set({ conflict_policy: e.target.value })}>
                  <option value="manual">Ask (manual)</option><option value="prefer_remote">Remote wins</option><option value="prefer_local">Local wins</option>
                </select></td>
                <td data-label="Remote deleted"><select value={d.delete_policy} onChange={(e) => set({ delete_policy: e.target.value })}>
                  <option value="keep">Keep local</option><option value="clear">Clear local</option>
                </select></td>
                <td data-label="Status">
                  {m ? <span className={`badge status-${m.status}`} title={m.message ?? ''}>{m.status}</span> : <span className="muted">new</span>}
                  {m?.status === 'conflict' && (
                    <div className="row small">
                      <button onClick={() => resolve(m, 'accept_remote')}>Use remote</button>
                      <button onClick={() => resolve(m, 'keep_local')}>Keep local</button>
                    </div>
                  )}
                </td>
                <td className="actions"><button className="icon-btn danger" title="Remove mapping" onClick={() => setDraft(draft.filter((_, j) => j !== i))}><Trash size={14} /></button></td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <button disabled={!providers.length} onClick={() => setDraft([...draft, { field: 'password', provider_id: providers[0]?.id ?? '', path: '/', key: '',
        conflict_policy: 'manual', delete_policy: 'keep' }])}><Plus size={14} /> Add mapping</button>
      {browsing !== null && draft[browsing] && providers.find((p) => p.id === draft[browsing].provider_id) && (
        <SecretBrowser provider={providers.find((p) => p.id === draft[browsing].provider_id)!}
                       initialPath={draft[browsing].path === '/' ? '' : draft[browsing].path}
                       onClose={() => setBrowsing(null)}
                       onPick={(path, key) => {
                         setDraft(draft.map((x, j) => (j === browsing ? { ...x, path, key } : x)));
                         setBrowsing(null);
                       }} />
      )}
    </Modal>
  );
}

type FolderSel = 'all' | 'none' | string;

function FolderTree({ folders, selected, onSelect }: {
  folders: CredentialFolder[]; selected: FolderSel; onSelect: (f: FolderSel) => void;
}) {
  const children = (parent: string | null) => folders.filter((f) => (f.parent_id ?? null) === parent)
    .sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' }));
  const render = (parent: string | null, depth: number): React.ReactNode => children(parent).map((f) => (
    <li key={f.id}>
      <button className={`cred-folder ${selected === f.id ? 'active' : ''}`} style={{ paddingLeft: 8 + depth * 14 }}
              onClick={() => onSelect(f.id)} title={f.description ?? f.name}>
        <Folder size={14} /> <span>{f.name}</span>
      </button>
      <ul>{render(f.id, depth + 1)}</ul>
    </li>
  ));
  return (
    <nav className="cred-folders" aria-label="Credential folders">
      <ul>
        <li><button className={`cred-folder ${selected === 'all' ? 'active' : ''}`} onClick={() => onSelect('all')}>
          <Layers size={14} /> <span>All credentials</span></button></li>
        <li><button className={`cred-folder ${selected === 'none' ? 'active' : ''}`} onClick={() => onSelect('none')}>
          <KeyRound size={14} /> <span>No folder</span></button></li>
        {render(null, 0)}
      </ul>
    </nav>
  );
}

export function CredentialsView() {
  const { data: me } = useMe();
  const { data, error, isLoading } = useCredentials();
  const { data: folderData } = useCredentialFolders();
  const folders = folderData ?? [];
  const invalidate = useInvalidate();
  const isMobile = useMediaQuery(MOBILE_QUERY);
  const [q, setQ] = useState('');
  const [folder, setFolder] = useState<FolderSel>('all');
  const [editing, setEditing] = useState<Credential | 'new' | null>(null);
  const [revealing, setRevealing] = useState<Credential | null>(null);
  const [mapping, setMapping] = useState<Credential | null>(null);
  const [acl, setAcl] = useState<{ type: 'credential' | 'cred_folder'; id: string; name: string } | null>(null);
  const current = folder !== 'all' && folder !== 'none' ? folders.find((f) => f.id === folder) ?? null : null;
  const inFolder = current ? folderSubtree(folders, current.id) : null;
  const items = (data?.items ?? [])
    .filter((c) => folder === 'all' || (folder === 'none' ? !c.folder_id : Boolean(c.folder_id && inFolder?.has(c.folder_id))))
    .filter((c) => !q || `${c.name} ${c.username ?? ''} ${c.description ?? ''}`.toLowerCase().includes(q.toLowerCase()));
  const canCreate = hasPerm(me, 'credentials.create');
  const folderEditable = current ? ['edit', 'manage'].includes(current.permission ?? '') : canCreate;

  const remove = async (c: Credential) => {
    if (!(await dialogs.confirm({ title: 'Delete credential', message: `Delete "${c.name}"?`, danger: true, confirmLabel: 'Delete' }))) return;
    try {
      await del(`/api/v1/credentials/${c.id}`);
      invalidate.credentials();
    } catch (err) {
      dialogs.toast(errorMessage(err), 'error');
    }
  };

  const newFolder = async () => {
    const name = await dialogs.prompt({ title: current ? `New folder in ${current.name}` : 'New folder', label: 'Name' });
    if (!name?.trim()) return;
    try {
      const f = await post<CredentialFolder>('/api/v1/credential-folders', { name: name.trim(), parent_id: current?.id ?? null });
      invalidate.credentials();
      setFolder(f.id);
    } catch (err) {
      dialogs.toast(errorMessage(err), 'error');
    }
  };

  const renameFolder = async () => {
    if (!current) return;
    const name = await dialogs.prompt({ title: 'Rename folder', label: 'Name', initial: current.name });
    if (!name?.trim() || name.trim() === current.name) return;
    try {
      await patch(`/api/v1/credential-folders/${current.id}`, { name: name.trim(), version: current.version });
      invalidate.credentials();
    } catch (err) {
      dialogs.toast(errorMessage(err), 'error');
    }
  };

  const deleteFolder = async () => {
    if (!current) return;
    const ok = await dialogs.confirm({
      title: 'Delete folder',
      message: `Delete the folder "${current.name}"? Its credentials and sub-folders move up one level; no credential is deleted.`,
      danger: true, confirmLabel: 'Delete folder',
    });
    if (!ok) return;
    try {
      await del(`/api/v1/credential-folders/${current.id}`, { move_contents: true });
      setFolder(current.parent_id ?? 'all');
      invalidate.credentials();
    } catch (err) {
      dialogs.toast(errorMessage(err), 'error');
    }
  };

  return (
    <div className="page">
      <div className="page-head">
        <h1><KeyRound size={18} /> Credential store</h1>
        <div className="search"><Search size={14} /><input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter…" /></div>
        <button className="primary" disabled={!canCreate || !folderEditable} onClick={() => setEditing('new')}><Plus size={14} /> New credential</button>
      </div>
      {error && <div className="banner error">{errorMessage(error)}</div>}
      <div className="cred-layout">
        <aside>
          {isMobile ? (
            <select className="cred-folder-select" value={folder} onChange={(e) => setFolder(e.target.value)} aria-label="Folder">
              <option value="all">All credentials</option>
              <option value="none">No folder</option>
              {[...folders].sort((a, b) => folderPath(folders, a.id).localeCompare(folderPath(folders, b.id)))
                .map((f) => <option key={f.id} value={f.id}>{folderPath(folders, f.id)}</option>)}
            </select>
          ) : (
            <FolderTree folders={folders} selected={folder} onSelect={setFolder} />
          )}
          <div className="row small">
            <button disabled={!canCreate || (current !== null && !folderEditable)} onClick={newFolder} title={current ? 'New sub-folder' : 'New folder'}>
              <FolderPlus size={14} /> Folder</button>
          </div>
        </aside>
        <section>
          {current && (
            <div className="row cred-folder-head">
              <b><Folder size={14} /> {folderPath(folders, current.id)}</b>
              {current.description && <span className="muted small">{current.description}</span>}
              <span className="spacer" />
              <button className="icon-btn" title="Rename" disabled={!folderEditable} onClick={renameFolder}><Pencil size={14} /></button>
              <button className="icon-btn" title="Permissions (apply to everything in this folder)" disabled={current.permission !== 'manage'}
                      onClick={() => setAcl({ type: 'cred_folder', id: current.id, name: current.name })}><Shield size={14} /></button>
              <button className="icon-btn danger" title="Delete folder" disabled={current.permission !== 'manage'} onClick={deleteFolder}><Trash size={14} /></button>
            </div>
          )}
          {isLoading ? <p className="muted">Loading…</p> : (
            <table className="grid stack">
              <thead><tr><th>Name</th><th>Username</th><th>Folder</th><th>Secrets</th><th>Used by</th><th>External</th><th>Changed</th><th /></tr></thead>
              <tbody>
                {items.map((c) => (
                  <tr key={c.id}>
                    <td className="stack-title"><b>{c.name}</b>{c.description && <div className="muted small">{c.description}</div>}</td>
                    <td data-label="Username">{c.domain ? `${c.domain}\\` : ''}{c.username ?? <span className="muted">—</span>}</td>
                    <td data-label="Folder" className="small muted">{c.folder_id ? folderPath(folders, c.folder_id) : '—'}</td>
                    <td data-label="Secrets" className="small">
                      {[c.password_set && 'password', c.private_key_set && 'key', c.passphrase_set && 'passphrase'].filter(Boolean).join(', ') || <span className="muted">none</span>}
                      {Object.keys(c.attributes ?? {}).length > 0 && <div className="muted">{Object.keys(c.attributes).join(', ')}</div>}
                      {c.rotation_due && <span className="badge warn">rotation due</span>}
                    </td>
                    <td data-label="Used by">{c.usage_count}</td>
                    <td data-label="External" className={c.external_fields.length ? '' : 'stack-empty'}>
                      {c.external_fields.length ? <span className="badge"><Cloud size={11} /> {c.external_fields.join(', ')}</span> : ''}</td>
                    <td data-label="Changed" className="small muted">{formatDate(c.secret_changed_at ?? c.updated_at)}</td>
                    <td className="actions">
                      <button className="icon-btn" title="Edit" disabled={!['edit', 'manage'].includes(c.permission ?? '')} onClick={() => setEditing(c)}><Pencil size={14} /></button>
                      <button className="icon-btn" title="Reveal secrets" disabled={c.permission !== 'manage' || !hasPerm(me, 'credentials.reveal')} onClick={() => setRevealing(c)}><Eye size={14} /></button>
                      <button className="icon-btn" title="External secrets" disabled={c.permission !== 'manage'} onClick={() => setMapping(c)}><Cloud size={14} /></button>
                      <button className="icon-btn" title="Permissions" disabled={c.permission !== 'manage'} onClick={() => setAcl({ type: 'credential', id: c.id, name: c.name })}><Shield size={14} /></button>
                      <button className="icon-btn danger" title="Delete" disabled={c.permission !== 'manage'} onClick={() => remove(c)}><Trash size={14} /></button>
                    </td>
                  </tr>
                ))}
                {!items.length && <tr><td colSpan={8} className="muted">No credentials{current ? ' in this folder' : ''}.</td></tr>}
              </tbody>
            </table>
          )}
        </section>
      </div>
      {editing && <CredentialDialog credential={editing === 'new' ? undefined : editing} all={data?.items ?? []} folders={folders}
                                    defaultFolder={current?.id ?? null} onClose={() => setEditing(null)} />}
      {revealing && <RevealDialog credential={revealing} onClose={() => setRevealing(null)} />}
      {mapping && <MappingsDialog credential={mapping} onClose={() => { setMapping(null); invalidate.credentials(); }} />}
      {acl && <AclDialog resourceType={acl.type} resourceId={acl.id} name={acl.name} onClose={() => setAcl(null)} />}
    </div>
  );
}
