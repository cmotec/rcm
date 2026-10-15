import { Cloud, Eye, KeyRound, Pencil, Plus, Search, Shield, Trash, TriangleAlert } from 'lucide-react';
import { useEffect, useState } from 'react';
import { del, errorMessage, get, patch, post, put } from '../../api/client';
import { hasPerm, useCredentials, useInvalidate, useMe } from '../../api/hooks';
import type { Credential, CredentialSecrets, SecretMapping, SecretProvider } from '../../api/types';
import { dialogs } from '../../state/dialogs';
import { formatDate } from '../../util/download';
import { AclDialog } from '../dialogs/AclDialog';
import { Modal } from '../common/Modal';
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

function CredentialDialog({ credential, all, onClose }: { credential?: Credential; all: Credential[]; onClose: () => void }) {
  const invalidate = useInvalidate();
  const [name, setName] = useState(credential?.name ?? '');
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
      {!providers.length && <p className="muted">No secret providers are configured. An administrator can add Infisical or OpenBao under Administration → Secret providers.</p>}
      <table className="grid">
        <thead><tr><th>Field</th><th>Provider</th><th>Path</th><th>Key</th><th>On conflict</th><th>On remote delete</th><th>Status</th><th /></tr></thead>
        <tbody>
          {draft.map((d, i) => {
            const m = mappings.find((x) => x.field === d.field);
            const set = (patchObj: Partial<typeof d>) => setDraft(draft.map((x, j) => (j === i ? { ...x, ...patchObj } : x)));
            return (
              <tr key={i}>
                <td><select value={d.field} onChange={(e) => set({ field: e.target.value })}>
                  {['username', 'password', 'domain', 'private_key', 'passphrase'].map((f) => <option key={f}>{f}</option>)}
                </select></td>
                <td><select value={d.provider_id} onChange={(e) => set({ provider_id: e.target.value })}>
                  {providers.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
                </select></td>
                <td>
                  <div className="row nowrap">
                    <input value={d.path} onChange={(e) => set({ path: e.target.value })} placeholder="/ or secret/path" />
                    <button className="icon-btn" title="Browse the provider" disabled={!d.provider_id} onClick={() => setBrowsing(i)}>
                      <Search size={14} />
                    </button>
                  </div>
                </td>
                <td><input value={d.key} onChange={(e) => set({ key: e.target.value })} placeholder="(no name)"
                           title="Name of the key inside the secret; leave empty for a key without a name" /></td>
                <td><select value={d.conflict_policy} onChange={(e) => set({ conflict_policy: e.target.value })}>
                  <option value="manual">Ask (manual)</option><option value="prefer_remote">Remote wins</option><option value="prefer_local">Local wins</option>
                </select></td>
                <td><select value={d.delete_policy} onChange={(e) => set({ delete_policy: e.target.value })}>
                  <option value="keep">Keep local</option><option value="clear">Clear local</option>
                </select></td>
                <td>
                  {m ? <span className={`badge status-${m.status}`} title={m.message ?? ''}>{m.status}</span> : <span className="muted">new</span>}
                  {m?.status === 'conflict' && (
                    <div className="row small">
                      <button onClick={() => resolve(m, 'accept_remote')}>Use remote</button>
                      <button onClick={() => resolve(m, 'keep_local')}>Keep local</button>
                    </div>
                  )}
                </td>
                <td><button className="icon-btn danger" onClick={() => setDraft(draft.filter((_, j) => j !== i))}><Trash size={14} /></button></td>
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

export function CredentialsView() {
  const { data: me } = useMe();
  const { data, error, isLoading } = useCredentials();
  const invalidate = useInvalidate();
  const [q, setQ] = useState('');
  const [editing, setEditing] = useState<Credential | 'new' | null>(null);
  const [revealing, setRevealing] = useState<Credential | null>(null);
  const [mapping, setMapping] = useState<Credential | null>(null);
  const [acl, setAcl] = useState<Credential | null>(null);
  const items = (data?.items ?? []).filter((c) => !q || `${c.name} ${c.username ?? ''} ${c.description ?? ''}`.toLowerCase().includes(q.toLowerCase()));

  const remove = async (c: Credential) => {
    if (!(await dialogs.confirm({ title: 'Delete credential', message: `Delete "${c.name}"?`, danger: true, confirmLabel: 'Delete' }))) return;
    try {
      await del(`/api/v1/credentials/${c.id}`);
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
        <button className="primary" disabled={!hasPerm(me, 'credentials.create')} onClick={() => setEditing('new')}><Plus size={14} /> New credential</button>
      </div>
      {error && <div className="banner error">{errorMessage(error)}</div>}
      {isLoading ? <p className="muted">Loading…</p> : (
        <table className="grid">
          <thead><tr><th>Name</th><th>Username</th><th>Secrets</th><th>Used by</th><th>External</th><th>Changed</th><th /></tr></thead>
          <tbody>
            {items.map((c) => (
              <tr key={c.id}>
                <td><b>{c.name}</b>{c.description && <div className="muted small">{c.description}</div>}</td>
                <td>{c.domain ? `${c.domain}\\` : ''}{c.username ?? <span className="muted">—</span>}</td>
                <td className="small">
                  {[c.password_set && 'password', c.private_key_set && 'key', c.passphrase_set && 'passphrase'].filter(Boolean).join(', ') || <span className="muted">none</span>}
                  {c.rotation_due && <span className="badge warn">rotation due</span>}
                </td>
                <td>{c.usage_count}</td>
                <td>{c.external_fields.length ? <span className="badge"><Cloud size={11} /> {c.external_fields.join(', ')}</span> : ''}</td>
                <td className="small muted">{formatDate(c.secret_changed_at ?? c.updated_at)}</td>
                <td className="actions">
                  <button className="icon-btn" title="Edit" disabled={!['edit', 'manage'].includes(c.permission ?? '')} onClick={() => setEditing(c)}><Pencil size={14} /></button>
                  <button className="icon-btn" title="Reveal secrets" disabled={c.permission !== 'manage' || !hasPerm(me, 'credentials.reveal')} onClick={() => setRevealing(c)}><Eye size={14} /></button>
                  <button className="icon-btn" title="External secrets" disabled={c.permission !== 'manage'} onClick={() => setMapping(c)}><Cloud size={14} /></button>
                  <button className="icon-btn" title="Permissions" disabled={c.permission !== 'manage'} onClick={() => setAcl(c)}><Shield size={14} /></button>
                  <button className="icon-btn danger" title="Delete" disabled={c.permission !== 'manage'} onClick={() => remove(c)}><Trash size={14} /></button>
                </td>
              </tr>
            ))}
            {!items.length && <tr><td colSpan={7} className="muted">No credentials.</td></tr>}
          </tbody>
        </table>
      )}
      {editing && <CredentialDialog credential={editing === 'new' ? undefined : editing} all={data?.items ?? []} onClose={() => setEditing(null)} />}
      {revealing && <RevealDialog credential={revealing} onClose={() => setRevealing(null)} />}
      {mapping && <MappingsDialog credential={mapping} onClose={() => { setMapping(null); invalidate.credentials(); }} />}
      {acl && <AclDialog resourceType="credential" resourceId={acl.id} name={acl.name} onClose={() => setAcl(null)} />}
    </div>
  );
}
