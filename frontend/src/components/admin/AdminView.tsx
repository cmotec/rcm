import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Plus, RefreshCw, Trash } from 'lucide-react';
import { useState } from 'react';
import { del, errorMessage, get, patch, post } from '../../api/client';
import { hasPerm, useMe, useTree } from '../../api/hooks';
import type { AuditEvent, Connection, KnownHost, Page, Role, SecretProvider, Tree, User } from '../../api/types';
import { dialogs } from '../../state/dialogs';
import { formatDate } from '../../util/download';
import { Modal } from '../common/Modal';
import { ImportWizard } from './ImportWizard';

type Section = 'users' | 'roles' | 'hosts' | 'secrets' | 'import' | 'audit' | 'settings';

async function guard(fn: () => Promise<unknown>, ok?: string) {
  try {
    await fn();
    if (ok) dialogs.toast(ok, 'success');
  } catch (err) {
    dialogs.toast(errorMessage(err), 'error');
  }
}

// --------------------------------------------------------------------------- users

function UsersSection() {
  const qc = useQueryClient();
  const [q, setQ] = useState('');
  const users = useQuery({ queryKey: ['users', q], queryFn: () => get<Page<User>>('/api/v1/users', { q, limit: 500 }) });
  const roles = useQuery({ queryKey: ['roles'], queryFn: () => get<Role[]>('/api/v1/roles') });
  const [editing, setEditing] = useState<User | 'new' | null>(null);
  const refresh = () => void qc.invalidateQueries({ queryKey: ['users'] });
  return (
    <>
      <div className="row">
        <input placeholder="Search users…" value={q} onChange={(e) => setQ(e.target.value)} />
        <button className="primary" onClick={() => setEditing('new')}><Plus size={14} /> Local user</button>
      </div>
      <table className="grid">
        <thead><tr><th>User</th><th>Roles</th><th>IdP groups</th><th>Type</th><th>Last login</th><th>Status</th><th /></tr></thead>
        <tbody>
          {users.data?.items.map((u) => (
            <tr key={u.id} className={u.is_active ? '' : 'disabled'}>
              <td><b>{u.username}</b><div className="muted small">{u.display_name} {u.email}</div></td>
              <td>{u.is_signage && <span className="badge warn">signage</span>}
                {u.roles.map((r) => <span key={r} className="badge">{r}</span>)}</td>
              <td className="small">{u.groups.join(', ')}</td>
              <td>{u.is_local ? 'local' : 'OIDC'}</td>
              <td className="small">{formatDate(u.last_login_at)}</td>
              <td>{u.is_active ? 'active' : 'disabled'}</td>
              <td className="actions"><button onClick={() => setEditing(u)}>Edit</button></td>
            </tr>
          ))}
        </tbody>
      </table>
      {editing && <UserDialog user={editing === 'new' ? undefined : editing} roles={roles.data ?? []}
                              onClose={() => { setEditing(null); refresh(); }} />}
    </>
  );
}

/** RDP/VNC connections for the signage picker, labelled with their folder path. */
function signageCandidates(tree: Tree | undefined): { id: string; label: string }[] {
  if (!tree) return [];
  const folders = new Map(tree.folders.map((f) => [f.id, f]));
  const conns = new Map(tree.connections.map((c) => [c.id, c]));
  const pathOf = (c: Connection): string => {
    let top = c;
    for (let i = 0; top.parent_id && conns.has(top.parent_id) && i < 64; i++) top = conns.get(top.parent_id)!;
    const parts: string[] = [];
    for (let f = top.folder_id ? folders.get(top.folder_id) : undefined, i = 0; f && i < 64; i++) {
      parts.unshift(f.name);
      f = f.parent_id ? folders.get(f.parent_id) : undefined;
    }
    if (top !== c) parts.push(top.name);
    return parts.join(' / ');
  };
  return tree.connections
    .filter((c) => c.protocol === 'rdp' || c.protocol === 'vnc')
    .map((c) => {
      const path = pathOf(c);
      return { id: c.id, label: `${path ? `${path} / ` : ''}${c.name} (${c.protocol.toUpperCase()}${c.hostname ? `, ${c.hostname}` : ''})` };
    })
    .sort((a, b) => a.label.localeCompare(b.label, undefined, { sensitivity: 'base', numeric: true }));
}

function UserDialog({ user, roles, onClose }: { user?: User; roles: Role[]; onClose: () => void }) {
  const [username, setUsername] = useState(user?.username ?? '');
  const [displayName, setDisplayName] = useState(user?.display_name ?? '');
  const [email, setEmail] = useState(user?.email ?? '');
  const [password, setPassword] = useState('');
  const [selected, setSelected] = useState<Set<string>>(new Set(user?.roles ?? ['user']));
  const [active, setActive] = useState(user?.is_active ?? true);
  const [signage, setSignage] = useState(user?.is_signage ?? false);
  const [signageConn, setSignageConn] = useState(user?.signage_connection_id ?? '');
  const tree = useTree();
  const screens = signageCandidates(tree.data);
  const [error, setError] = useState<string | null>(null);
  const save = async () => {
    const signageFields = { is_signage: signage, signage_connection_id: signage && signageConn ? signageConn : null };
    try {
      if (user) {
        await patch(`/api/v1/users/${user.id}`, { display_name: displayName || null, email: email || null, is_active: active,
          roles: [...selected], ...signageFields, ...(password ? { password } : {}) });
      } else {
        await post('/api/v1/users', { username, display_name: displayName || null, email: email || null,
          password: password || null, roles: [...selected], ...signageFields });
      }
      onClose();
    } catch (err) {
      setError(errorMessage(err));
    }
  };
  const remove = async () => {
    if (!user || !(await dialogs.confirm({ title: 'Delete user', message: `Delete ${user.username}?`, danger: true, confirmLabel: 'Delete' }))) return;
    await guard(() => del(`/api/v1/users/${user.id}`), 'User deleted');
    onClose();
  };
  return (
    <Modal title={user ? `User ${user.username}` : 'New local user'} onClose={onClose} width={520}
           footer={<>
             {error && <span className="form-error">{error}</span>}
             {user && <button className="danger" onClick={remove}>Delete</button>}
             <button onClick={onClose}>Cancel</button>
             <button className="primary" onClick={save}>Save</button>
           </>}>
      {!user && <label className="field"><span>Username</span><input value={username} onChange={(e) => setUsername(e.target.value)} autoFocus /></label>}
      <label className="field"><span>Display name</span><input value={displayName} onChange={(e) => setDisplayName(e.target.value)} /></label>
      <label className="field"><span>E-mail</span><input value={email} onChange={(e) => setEmail(e.target.value)} /></label>
      {(!user || user.is_local) && (
        <label className="field"><span>{user ? 'New password (leave empty to keep)' : 'Password (local login; leave empty for an OIDC user)'}</span>
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" />
        </label>
      )}
      <label className="check">
        <input type="checkbox" checked={signage} onChange={(e) => setSignage(e.target.checked)} />
        <span>Signage display</span>
        <small className="muted">Shows only the connection below, fullscreen and without menus. It starts after login and
          reconnects on its own. Roles and permissions do not apply. The connection needs stored credentials.</small>
      </label>
      {signage && (
        <label className="field"><span>Connection shown on the display (RDP or VNC)</span>
          <select value={signageConn} onChange={(e) => setSignageConn(e.target.value)}>
            <option value="">— none (the display shows a notice) —</option>
            {screens.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
          </select>
        </label>
      )}
      <div className={`field ${signage ? 'muted' : ''}`}><span>Roles (manually assigned; IdP-mapped roles are added at login)</span>
        {roles.map((r) => (
          <label key={r.id} className="check">
            <input type="checkbox" checked={selected.has(r.name)} onChange={(e) => {
              const s = new Set(selected);
              if (e.target.checked) s.add(r.name); else s.delete(r.name);
              setSelected(s);
            }} />
            <span>{r.name}</span><small className="muted">{r.description}</small>
          </label>
        ))}
      </div>
      {user && <label className="check"><input type="checkbox" checked={active} onChange={(e) => setActive(e.target.checked)} /><span>Active</span>
        <small className="muted">Deactivating ends all browser and remote sessions of the user.</small></label>}
    </Modal>
  );
}

// --------------------------------------------------------------------------- roles & group mappings

function RolesSection() {
  const qc = useQueryClient();
  const roles = useQuery({ queryKey: ['roles'], queryFn: () => get<Role[]>('/api/v1/roles') });
  const perms = useQuery({ queryKey: ['permissions'], queryFn: () => get<string[]>('/api/v1/permissions') });
  const mappings = useQuery({ queryKey: ['group-mappings'], queryFn: () => get<{ id: string; group_name: string; role_id: string }[]>('/api/v1/group-mappings') });
  const envMappings = useQuery({ queryKey: ['group-mappings-env'], queryFn: () => get<Record<string, string[]>>('/api/v1/group-mappings/env') });
  const [group, setGroup] = useState('');
  const [role, setRole] = useState('user');
  const [newRole, setNewRole] = useState<{ name: string; description: string; perms: Set<string>; cap: string } | null>(null);
  const roleName = (id: string) => roles.data?.find((r) => r.id === id)?.name ?? id;
  const refresh = () => void qc.invalidateQueries();
  return (
    <>
      <h3>Roles</h3>
      <table className="grid">
        <thead><tr><th>Role</th><th>Global permissions</th><th>Resource cap</th><th /></tr></thead>
        <tbody>
          {roles.data?.map((r) => (
            <tr key={r.id}>
              <td><b>{r.name}</b>{r.builtin && <span className="badge">built-in</span>}<div className="muted small">{r.description}</div></td>
              <td className="small">{r.permissions.join(', ')}</td>
              <td>{r.resource_cap}</td>
              <td>{!r.builtin && <button className="icon-btn danger" onClick={() => guard(() => del(`/api/v1/roles/${r.id}`)).then(refresh)}><Trash size={14} /></button>}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <button onClick={() => setNewRole({ name: '', description: '', perms: new Set(['api.access']), cap: 'connect' })}><Plus size={14} /> Custom role</button>

      <h3>Identity-provider group → role mapping</h3>
      <p className="muted">Configured via environment: {Object.entries(envMappings.data ?? {}).map(([r, g]) => `${r}: ${g.join(', ') || '—'}`).join(' · ')}</p>
      <table className="grid">
        <thead><tr><th>IdP group</th><th>Role</th><th /></tr></thead>
        <tbody>
          {mappings.data?.map((m) => (
            <tr key={m.id}><td>{m.group_name}</td><td>{roleName(m.role_id)}</td>
              <td><button className="icon-btn danger" onClick={() => guard(() => del(`/api/v1/group-mappings/${m.id}`)).then(refresh)}><Trash size={14} /></button></td></tr>
          ))}
        </tbody>
      </table>
      <div className="row">
        <input placeholder="group name (as sent by Authentik)" value={group} onChange={(e) => setGroup(e.target.value)} />
        <select value={role} onChange={(e) => setRole(e.target.value)}>{roles.data?.map((r) => <option key={r.id}>{r.name}</option>)}</select>
        <button disabled={!group} onClick={() => guard(() => post('/api/v1/group-mappings', { group_name: group, role }), 'Mapping added').then(() => { setGroup(''); refresh(); })}>Add mapping</button>
      </div>
      {newRole && (
        <Modal title="New custom role" onClose={() => setNewRole(null)}
               footer={<><button onClick={() => setNewRole(null)}>Cancel</button>
                 <button className="primary" onClick={() => guard(() => post('/api/v1/roles', { name: newRole.name, description: newRole.description,
                   permissions: [...newRole.perms], resource_cap: newRole.cap }), 'Role created').then(() => { setNewRole(null); refresh(); })}>Create</button></>}>
          <label className="field"><span>Name (lowercase)</span><input value={newRole.name} onChange={(e) => setNewRole({ ...newRole, name: e.target.value })} /></label>
          <label className="field"><span>Description</span><input value={newRole.description} onChange={(e) => setNewRole({ ...newRole, description: e.target.value })} /></label>
          <label className="field"><span>Highest resource permission</span>
            <select value={newRole.cap} onChange={(e) => setNewRole({ ...newRole, cap: e.target.value })}>
              {['view', 'connect', 'edit', 'manage'].map((c) => <option key={c}>{c}</option>)}
            </select>
          </label>
          <div className="field"><span>Global permissions</span>
            {perms.data?.filter((p) => p !== '*').map((p) => (
              <label key={p} className="check"><input type="checkbox" checked={newRole.perms.has(p)} onChange={(e) => {
                const s = new Set(newRole.perms);
                if (e.target.checked) s.add(p); else s.delete(p);
                setNewRole({ ...newRole, perms: s });
              }} /><span className="mono">{p}</span></label>
            ))}
          </div>
        </Modal>
      )}
    </>
  );
}

// --------------------------------------------------------------------------- known hosts

function HostsSection() {
  const qc = useQueryClient();
  const [q, setQ] = useState('');
  const hosts = useQuery({ queryKey: ['known-hosts', q], queryFn: () => get<Page<KnownHost>>('/api/v1/known-hosts', { q, limit: 500 }) });
  const [adding, setAdding] = useState(false);
  const [form, setForm] = useState({ hostname: '', port: '22', public_key: '' });
  return (
    <>
      <div className="row">
        <input placeholder="Filter by host…" value={q} onChange={(e) => setQ(e.target.value)} />
        <button onClick={() => setAdding(true)}><Plus size={14} /> Pin host key</button>
      </div>
      <table className="grid">
        <thead><tr><th>Host</th><th>Key type</th><th>Fingerprint</th><th>Trusted since</th><th>Last seen</th><th /></tr></thead>
        <tbody>
          {hosts.data?.items.map((h) => (
            <tr key={h.id}>
              <td>{h.hostname}:{h.port}</td><td>{h.key_type}</td><td className="mono small">{h.fingerprint}</td>
              <td className="small">{formatDate(h.created_at)}</td><td className="small">{formatDate(h.last_seen_at)}</td>
              <td><button className="icon-btn danger" title="Remove (next connection asks again)"
                          onClick={() => guard(() => del(`/api/v1/known-hosts/${h.id}`)).then(() => qc.invalidateQueries({ queryKey: ['known-hosts'] }))}>
                <Trash size={14} /></button></td>
            </tr>
          ))}
        </tbody>
      </table>
      {adding && (
        <Modal title="Pin SSH host key" onClose={() => setAdding(false)}
               footer={<><button onClick={() => setAdding(false)}>Cancel</button>
                 <button className="primary" onClick={() => guard(() => post('/api/v1/known-hosts', { ...form, port: Number(form.port) }), 'Host key pinned')
                   .then(() => { setAdding(false); void qc.invalidateQueries({ queryKey: ['known-hosts'] }); })}>Save</button></>}>
          <label className="field"><span>Hostname</span><input value={form.hostname} onChange={(e) => setForm({ ...form, hostname: e.target.value })} /></label>
          <label className="field"><span>Port</span><input type="number" value={form.port} onChange={(e) => setForm({ ...form, port: e.target.value })} /></label>
          <label className="field"><span>Public key (e.g. contents of /etc/ssh/ssh_host_ed25519_key.pub)</span>
            <textarea rows={3} className="mono" value={form.public_key} onChange={(e) => setForm({ ...form, public_key: e.target.value })} /></label>
        </Modal>
      )}
    </>
  );
}

// --------------------------------------------------------------------------- secret providers

function SecretsSection() {
  const qc = useQueryClient();
  const providers = useQuery({ queryKey: ['secret-providers'], queryFn: () => get<SecretProvider[]>('/api/v1/secret-providers') });
  const [editing, setEditing] = useState<SecretProvider | 'new' | null>(null);
  const refresh = () => void qc.invalidateQueries({ queryKey: ['secret-providers'] });
  return (
    <>
      <p className="muted">Synchronise credential fields from Infisical or OpenBao. Synchronisation is one-way (external → local) and never
        silently overwrites local changes: conflicting values wait for a decision in the credential’s “External secrets” dialog.</p>
      <button className="primary" onClick={() => setEditing('new')}><Plus size={14} /> Add provider</button>
      <table className="grid">
        <thead><tr><th>Name</th><th>Type</th><th>Mappings</th><th>Last sync</th><th>Status</th><th /></tr></thead>
        <tbody>
          {providers.data?.map((p) => (
            <tr key={p.id}>
              <td><b>{p.name}</b>{p.managed_by_env && <span className="badge">environment</span>}{!p.enabled && <span className="badge">disabled</span>}</td>
              <td>{p.type}</td>
              <td>{p.mapping_count}{p.conflict_count > 0 && <span className="badge warn">{p.conflict_count} conflicts</span>}</td>
              <td className="small">{formatDate(p.last_sync_at)}</td>
              <td><span className={`badge status-${p.last_sync_status}`} title={p.last_sync_error ?? ''}>{p.last_sync_status}</span></td>
              <td className="actions">
                <button onClick={() => guard(async () => { const r = await post<Record<string, unknown>>(`/api/v1/secret-providers/${p.id}/test`); dialogs.toast(`Connection OK ${JSON.stringify(r)}`, 'success'); })}>Test</button>
                <button title="Synchronise now" onClick={() => guard(async () => { const r = await post<{ status: string; stats: Record<string, number> }>(`/api/v1/secret-providers/${p.id}/sync`);
                  dialogs.toast(`Sync ${r.status}: ${Object.entries(r.stats).map(([k, v]) => `${k} ${v}`).join(', ')}`, r.status === 'ok' ? 'success' : 'warning'); refresh(); })}>
                  <RefreshCw size={13} /></button>
                <button onClick={() => setEditing(p)}>Edit</button>
                {!p.managed_by_env && <button className="icon-btn danger" onClick={() => dialogs.confirm({ title: 'Remove provider', message: `Remove ${p.name} and all its mappings? Local values are kept.`, danger: true, confirmLabel: 'Remove' })
                  .then((ok) => ok && guard(() => del(`/api/v1/secret-providers/${p.id}`)).then(refresh))}><Trash size={14} /></button>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {editing && <ProviderDialog provider={editing === 'new' ? undefined : editing} onClose={() => { setEditing(null); refresh(); }} />}
    </>
  );
}

interface ProviderField {
  key: string;
  label: string;
  placeholder?: string;
  /** [value, label] pairs: shown as a dropdown */
  options?: [string, string][];
  defaultValue?: string;
  /** a checkbox (stored as true/false) */
  kind?: 'bool';
  hint?: string;
}

function ProviderDialog({ provider, onClose }: { provider?: SecretProvider; onClose: () => void }) {
  const [type, setType] = useState<'infisical' | 'openbao'>(provider?.type ?? 'openbao');
  const [name, setName] = useState(provider?.name ?? '');
  const [config, setConfig] = useState<Record<string, string>>(Object.fromEntries(Object.entries(provider?.config ?? {}).map(([k, v]) => [k, String(v ?? '')])));
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const [interval, setSyncInterval] = useState(String(provider?.sync_interval_seconds ?? 900));
  const [enabled, setEnabled] = useState(provider?.enabled ?? true);
  const [error, setError] = useState<string | null>(null);
  const locked = provider?.managed_by_env;
  const approle = type === 'openbao' && config.auth_method === 'approle';
  const configFields: ProviderField[] = type === 'infisical'
    ? [{ key: 'url', label: 'URL' }, { key: 'project_id', label: 'Project ID' }, { key: 'environment', label: 'Environment slug' }]
    : [
      { key: 'url', label: 'URL', placeholder: 'https://bao.example.com:8200' },
      { key: 'namespace', label: 'Namespace (optional)' },
      { key: 'mount', label: 'KV mount (name of the secrets engine)', placeholder: 'secret' },
      { key: 'kv_version', label: 'KV version', options: [['2', 'KV version 2'], ['1', 'KV version 1']], defaultValue: '2' },
      { key: 'auth_method', label: 'Auth method', options: [['token', 'Token'], ['approle', 'AppRole']], defaultValue: 'token' },
      ...(approle ? [{ key: 'approle_mount', label: 'AppRole mount path', placeholder: 'approle' }] : []),
      { key: 'auto_import', label: 'Create credentials automatically', kind: 'bool',
        hint: 'Each secret becomes a credential named after it. Keys such as username, password, domain, private_key '
          + 'are matched to the credential fields; a secret with a single key (also without a name) is its password.' },
      ...(config.auto_import === 'true'
        ? [{ key: 'import_path', label: 'Only secrets below this path (optional)', placeholder: 'e.g. servers/ (empty = whole mount)' }]
        : []),
    ];
  const secretFields = type === 'infisical' ? [['client_id', 'Universal Auth client ID'], ['client_secret', 'Client secret']]
    : approle ? [['role_id', 'AppRole role_id'], ['secret_id', 'AppRole secret_id']] : [['token', 'Token']];
  const save = async () => {
    const cfg: Record<string, unknown> = {};
    for (const f of configFields) {
      if (f.kind === 'bool') {
        cfg[f.key] = config[f.key] === 'true';
        continue;
      }
      const v = config[f.key] || f.defaultValue;
      if (v) cfg[f.key] = f.key === 'kv_version' ? Number(v) : v;
    }
    // only the credentials of the chosen auth method
    const shown = new Set(secretFields.map(([k]) => k));
    const sec = Object.fromEntries(Object.entries(secrets).filter(([k, v]) => v && shown.has(k)));
    try {
      if (provider) await patch(`/api/v1/secret-providers/${provider.id}`, locked ? { enabled, sync_interval_seconds: Number(interval) }
        : { name, config: cfg, secrets: sec, enabled, sync_interval_seconds: Number(interval) });
      else await post('/api/v1/secret-providers', { name, type, config: cfg, secrets: sec, enabled, sync_interval_seconds: Number(interval) });
      onClose();
    } catch (err) {
      setError(errorMessage(err));
    }
  };
  return (
    <Modal title={provider ? `Provider ${provider.name}` : 'Add secret provider'} onClose={onClose} width={520}
           footer={<>{error && <span className="form-error">{error}</span>}<button onClick={onClose}>Cancel</button><button className="primary" onClick={save}>Save</button></>}>
      {locked && <div className="banner info">Configured through environment variables; only enable/interval can be changed here.</div>}
      <label className="field"><span>Name</span><input value={name} disabled={locked} onChange={(e) => setName(e.target.value)} /></label>
      {!provider && <label className="field"><span>Type</span><select value={type} onChange={(e) => setType(e.target.value as 'infisical' | 'openbao')}>
        <option value="openbao">OpenBao (Vault-compatible)</option><option value="infisical">Infisical</option></select></label>}
      {configFields.map((f) => f.kind === 'bool' ? (
        <label key={f.key} className="check">
          <input type="checkbox" checked={config[f.key] === 'true'} disabled={locked}
                 onChange={(e) => setConfig({ ...config, [f.key]: String(e.target.checked) })} />
          <span>{f.label}</span>{f.hint && <small className="muted">{f.hint}</small>}
        </label>
      ) : (
        <label key={f.key} className="field"><span>{f.label}</span>
          {f.options ? (
            <select value={config[f.key] || f.defaultValue || ''} disabled={locked} onChange={(e) => setConfig({ ...config, [f.key]: e.target.value })}>
              {f.options.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          ) : (
            <input value={config[f.key] ?? ''} placeholder={f.placeholder} disabled={locked} onChange={(e) => setConfig({ ...config, [f.key]: e.target.value })} />
          )}
        </label>
      ))}
      {secretFields.map(([k, label]) => (
        <label key={k} className="field"><span>{label}{provider?.secret_fields_set.includes(k) ? ' (stored — leave empty to keep)' : ''}</span>
          <input type="password" value={secrets[k] ?? ''} disabled={locked} onChange={(e) => setSecrets({ ...secrets, [k]: e.target.value })} autoComplete="new-password" /></label>
      ))}
      <label className="field"><span>Sync interval (seconds)</span><input type="number" min={60} value={interval} onChange={(e) => setSyncInterval(e.target.value)} /></label>
      <label className="check"><input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} /><span>Enabled (scheduled sync)</span></label>
    </Modal>
  );
}

// --------------------------------------------------------------------------- audit

function AuditSection({ all }: { all: boolean }) {
  const [action, setAction] = useState('');
  const [outcome, setOutcome] = useState('');
  const [offset, setOffset] = useState(0);
  const events = useQuery({
    queryKey: ['audit', action, outcome, offset],
    queryFn: () => get<Page<AuditEvent>>('/api/v1/audit', { action: action || undefined, outcome: outcome || undefined, limit: 100, offset }),
  });
  return (
    <>
      {!all && <p className="muted">You can see your own activity only.</p>}
      <div className="row">
        <input placeholder="action, e.g. session.* or credential.reveal" value={action} onChange={(e) => { setAction(e.target.value); setOffset(0); }} />
        <select value={outcome} onChange={(e) => { setOutcome(e.target.value); setOffset(0); }}>
          <option value="">all outcomes</option><option>success</option><option>failure</option><option>denied</option>
        </select>
        <span className="muted">{events.data?.total ?? 0} events</span>
        <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 100))}>Newer</button>
        <button disabled={(events.data?.total ?? 0) <= offset + 100} onClick={() => setOffset(offset + 100)}>Older</button>
      </div>
      <table className="grid small">
        <thead><tr><th>Time</th><th>Actor</th><th>Action</th><th>Resource</th><th>Outcome</th><th>IP</th><th>Details</th></tr></thead>
        <tbody>
          {events.data?.items.map((e) => (
            <tr key={e.id} className={`outcome-${e.outcome}`}>
              <td>{formatDate(e.ts)}</td><td>{e.actor_name}</td><td className="mono">{e.action}</td>
              <td>{e.resource_type} <span className="muted mono">{e.resource_id?.slice(0, 8)}</span></td>
              <td>{e.outcome}</td><td>{e.ip}</td>
              <td className="mono details-cell" title={JSON.stringify(e.details, null, 1)}>{JSON.stringify(e.details)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

// --------------------------------------------------------------------------- global settings

function SettingsSection() {
  const qc = useQueryClient();
  const data = useQuery({ queryKey: ['app-settings'], queryFn: () => get<{ values: Record<string, unknown>; defaults: Record<string, unknown> }>('/api/v1/settings') });
  const v = data.data?.values ?? {};
  const d = data.data?.defaults ?? {};
  const set = (key: string, value: unknown) => guard(() => patch('/api/v1/settings', { [key]: value }), 'Saved').then(() => qc.invalidateQueries({ queryKey: ['app-settings'] }));
  return (
    <div className="form-grid">
      <label className="field"><span>Default RDP certificate policy (env default: {String(d.rdp_cert_policy_default)})</span>
        <select value={String(v.rdp_cert_policy_default ?? '')} onChange={(e) => set('rdp_cert_policy_default', e.target.value || null)}>
          <option value="validate">Validate</option><option value="tofu">Accept on first use</option><option value="ignore">Do not validate (insecure)</option>
        </select>
      </label>
      <label className="check"><input type="checkbox" checked={Boolean(v.rdp_cert_allow_insecure)} onChange={(e) => set('rdp_cert_allow_insecure', e.target.checked)} />
        <span>Allow insecure RDP certificate policies (per connection)</span>
        <small className="muted">Can be forced off with RDP_CERT_ALLOW_INSECURE=false.</small></label>
      <label className="field"><span>Default SSH host key policy (env default: {String(d.ssh_host_key_policy_default)})</span>
        <select value={String(v.ssh_host_key_policy_default ?? '')} onChange={(e) => set('ssh_host_key_policy_default', e.target.value || null)}>
          <option value="prompt">Ask on first use</option><option value="strict">Strict</option><option value="accept-new">Accept new automatically</option>
        </select>
      </label>
      <label className="check"><input type="checkbox" checked={Boolean(v.ftp_tls_verify_default)} onChange={(e) => set('ftp_tls_verify_default', e.target.checked)} />
        <span>Validate FTPS certificates by default</span></label>
      <label className="field span2"><span>Banner message (shown to all users)</span>
        <input defaultValue={String(v.banner_message ?? '')} onBlur={(e) => set('banner_message', e.target.value || null)} />
      </label>
    </div>
  );
}

export function AdminView() {
  const { data: me } = useMe();
  const sections: { id: Section; label: string; perm: string }[] = [
    { id: 'users', label: 'Users', perm: 'users.manage' },
    { id: 'roles', label: 'Roles & group mapping', perm: 'roles.manage' },
    { id: 'hosts', label: 'SSH known hosts', perm: 'known_hosts.manage' },
    { id: 'secrets', label: 'Secret providers', perm: 'secrets.manage' },
    { id: 'import', label: 'Import (RDM)', perm: 'import.run' },
    { id: 'audit', label: 'Audit log', perm: 'api.access' },
    { id: 'settings', label: 'Global settings', perm: 'settings.manage' },
  ];
  const visible = sections.filter((s) => hasPerm(me, s.perm));
  const [section, setSection] = useState<Section>(visible[0]?.id ?? 'audit');
  return (
    <div className="page admin">
      <nav className="admin-nav">
        {visible.map((s) => <button key={s.id} className={section === s.id ? 'active' : ''} onClick={() => setSection(s.id)}>{s.label}</button>)}
      </nav>
      <div className="admin-body">
        {section === 'users' && <UsersSection />}
        {section === 'roles' && <RolesSection />}
        {section === 'hosts' && <HostsSection />}
        {section === 'secrets' && <SecretsSection />}
        {section === 'import' && <ImportWizard />}
        {section === 'audit' && <AuditSection all={hasPerm(me, 'audit.read')} />}
        {section === 'settings' && <SettingsSection />}
      </div>
    </div>
  );
}
