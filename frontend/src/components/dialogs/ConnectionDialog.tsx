import { CornerDownRight, RotateCcw, ShieldAlert } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { errorMessage, get, patch, post } from '../../api/client';
import { useCredentials, useInvalidate, useTree } from '../../api/hooks';
import type { AuthMode, Connection, EffectiveConnection, Protocol, ResolvedValue } from '../../api/types';
import { dialogs } from '../../state/dialogs';
import { PROTOCOL_LABELS } from '../common/icons';
import { Modal } from '../common/Modal';
import { type FieldSpec, PROTOCOL_FIELDS, cleanSettings, defaultPort } from './protocolFields';

interface Props {
  connection?: Connection;
  placement?: { folderId?: string | null; parentId?: string | null };
  onClose: () => void;
}

const AUTH_MODES: { value: AuthMode; label: string; help: string }[] = [
  { value: 'inherit', label: 'Inherit', help: 'Use the credentials of the parent connection.' },
  { value: 'direct', label: 'Connection-specific', help: 'Username, password and domain stored on this connection.' },
  { value: 'credential', label: 'Credential store', help: 'Reference an entry from the credential store.' },
  { value: 'interactive', label: 'Ask when connecting', help: 'Prompt for username and password; nothing is stored.' },
];

function sourceLabel(r: ResolvedValue | null | undefined): string {
  if (!r) return '';
  const s = r.source;
  if (s.kind === 'default') return 'protocol default';
  if (s.kind === 'environment') return 'server default (DNS_SEARCH_DOMAINS)';
  return `${s.kind === 'folder' ? 'folder' : s.kind === 'credential' ? 'credential' : ''} “${s.name ?? s.id}”`.trim();
}

function Inherited({ value, from, onReset }: { value?: string | null; from?: string; onReset?: () => void }) {
  if (onReset) {
    return (
      <span className="inherit-hint explicit">
        explicit value{from ? ` (overrides ${from})` : ''}
        <button type="button" className="link-btn" onClick={onReset} title="Clear to inherit"><RotateCcw size={11} /> inherit</button>
      </span>
    );
  }
  if (!value) return null;
  return <span className="inherit-hint"><CornerDownRight size={11} /> inherited: <b>{value}</b>{from ? ` from ${from}` : ''}</span>;
}

export function ConnectionDialog({ connection, placement, onClose }: Props) {
  const editing = Boolean(connection);
  const { data: tree } = useTree();
  const { data: creds } = useCredentials();
  const invalidate = useInvalidate();
  const parentId = connection?.parent_id ?? placement?.parentId ?? null;
  const folderId = connection?.folder_id ?? placement?.folderId ?? null;
  const parent = tree?.connections.find((c) => c.id === parentId);
  const folder = tree?.folders.find((f) => f.id === folderId);

  const [tab, setTab] = useState<'general' | 'auth' | 'settings'>('general');
  const [name, setName] = useState(connection?.name ?? '');
  const [protocol, setProtocol] = useState<Protocol>(connection?.protocol ?? (parent ? 'ssh' : 'ssh'));
  const [hostname, setHostname] = useState(connection?.hostname ?? '');
  const [port, setPort] = useState(connection?.port ? String(connection.port) : '');
  const [searchDomain, setSearchDomain] = useState(connection?.search_domain ?? '');
  const [description, setDescription] = useState(connection?.description ?? '');
  const [tags, setTags] = useState((connection?.tags ?? []).join(', '));
  const [authMode, setAuthMode] = useState<AuthMode>(connection?.auth_mode ?? 'inherit');
  const [inheritAuth, setInheritAuth] = useState(connection?.inherit_auth ?? true);
  const [username, setUsername] = useState(connection?.username ?? '');
  const [domain, setDomain] = useState(connection?.domain ?? '');
  const [password, setPassword] = useState<string | null>(null); // null = unchanged
  const [credentialId, setCredentialId] = useState(connection?.credential_id ?? '');
  const [settings, setSettings] = useState<Record<string, unknown>>(() => {
    const s = { ...(connection?.settings ?? {}) };
    if (Array.isArray(s.cert_fingerprints)) s.cert_fingerprints = (s.cert_fingerprints as string[]).join('\n');
    if (typeof s.tls_verify === 'boolean') s.tls_verify = String(s.tls_verify);
    if (s.color_depth !== undefined) s.color_depth = String(s.color_depth);
    return s;
  });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Inherited values: the parent's effective values (or this connection's own, when editing).
  const [inherited, setInherited] = useState<EffectiveConnection | null>(null);
  const [effective, setEffective] = useState<EffectiveConnection | null>(null);
  useEffect(() => {
    if (parentId) get<EffectiveConnection>(`/api/v1/connections/${parentId}/effective`).then(setInherited).catch(() => undefined);
    if (connection) get<EffectiveConnection>(`/api/v1/connections/${connection.id}/effective`).then(setEffective).catch(() => undefined);
  }, [parentId, connection]);

  const sshConnections = useMemo(() => (tree?.connections ?? []).filter((c) => c.protocol === 'ssh' && c.id !== connection?.id), [tree, connection]);
  const fields = PROTOCOL_FIELDS[protocol];
  const groups = useMemo(() => [...new Set(fields.map((f) => f.group))], [fields]);
  const portDefault = defaultPort(protocol, settings);
  const inheritedHost = inherited?.hostname?.value ?? null;
  const inheritedSearch = inherited?.search_domains?.value?.join(', ') ?? null;
  const parentName = parent?.name;

  const setField = (k: string, v: unknown) => setSettings((s) => ({ ...s, [k]: v }));

  const renderField = (f: FieldSpec) => {
    if (f.showIf && !f.showIf(settings, protocol)) return null;
    const raw = settings[f.key];
    const value = raw ?? f.defaultValue ?? '';
    let input: React.ReactNode;
    switch (f.type) {
      case 'bool':
        return (
          <label key={f.key} className="check">
            <input type="checkbox" checked={Boolean(raw ?? f.defaultValue)} onChange={(e) => setField(f.key, e.target.checked)} />
            <span>{f.label}</span>
            {f.help && <small className="muted">{f.help}</small>}
          </label>
        );
      case 'select':
        input = (
          <select value={String(value)} onChange={(e) => setField(f.key, e.target.value)}>
            {f.options!.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
        );
        break;
      case 'number':
        input = <input type="number" value={String(raw ?? '')} placeholder={f.placeholder ?? (f.defaultValue !== undefined ? String(f.defaultValue) : '')}
                       onChange={(e) => setField(f.key, e.target.value)} />;
        break;
      case 'textarea':
      case 'list':
        input = <textarea rows={3} value={String(raw ?? '')} placeholder={f.placeholder} onChange={(e) => setField(f.key, e.target.value)} />;
        break;
      case 'ssh-connection':
      case 'tunnel': {
        const current = f.type === 'tunnel' ? (raw as { via_connection_id?: string } | undefined)?.via_connection_id ?? '' : String(raw ?? '');
        input = (
          <select value={current} onChange={(e) => setField(f.key, f.type === 'tunnel' ? (e.target.value ? { via_connection_id: e.target.value } : null) : e.target.value)}>
            <option value="">None</option>
            {sshConnections.map((c) => <option key={c.id} value={c.id}>{c.name}{c.hostname ? ` (${c.hostname})` : ''}</option>)}
          </select>
        );
        break;
      }
      default:
        input = <input value={String(raw ?? '')} placeholder={f.placeholder ?? (f.defaultValue !== undefined ? String(f.defaultValue) : '')}
                       onChange={(e) => setField(f.key, e.target.value)} />;
    }
    return (
      <label key={f.key} className="field">
        <span>{f.label}</span>
        {input}
        {f.help && <small className="muted">{f.help}</small>}
      </label>
    );
  };

  const save = async () => {
    setSaving(true);
    setError(null);
    const body: Record<string, unknown> = {
      name: name.trim(),
      hostname: hostname.trim() || null,
      port: port ? Number(port) : null,
      search_domain: searchDomain.trim() || null,
      description: description.trim() || null,
      tags: tags.split(',').map((t) => t.trim()).filter(Boolean),
      auth_mode: authMode,
      inherit_auth: inheritAuth,
      settings: cleanSettings(protocol, settings),
    };
    if (authMode === 'direct' || authMode === 'interactive') {
      body.username = username.trim() || null;
      body.domain = domain.trim() || null;
    }
    if (authMode === 'direct' && password !== null) body.password = password || null;
    if (authMode === 'credential') body.credential_id = credentialId || null;
    try {
      if (connection) {
        if (protocol !== connection.protocol) body.protocol = protocol;
        await patch(`/api/v1/connections/${connection.id}`, { ...body, version: connection.version });
        dialogs.toast('Connection saved', 'success');
      } else {
        await post('/api/v1/connections', {
          ...body,
          protocol,
          ...(parentId ? { parent_id: parentId } : folderId ? { folder_id: folderId } : {}),
        });
        dialogs.toast('Connection created', 'success');
      }
      invalidate.tree();
      onClose();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  };

  const authRow = (key: 'username' | 'password' | 'domain', label: string) => {
    const r = (effective ?? inherited)?.auth?.[key];
    if (!r) return <dd className="muted">not set</dd>;
    return (
      <dd>
        {key === 'password' ? (r.set ? '•••••• (set)' : 'not set') : r.value}
        <span className="muted"> — {r.source.kind === 'interactive' ? 'entered at launch' : `${label} from ${sourceLabel(r)}`}{r.inherited ? ' (inherited)' : ''}</span>
      </dd>
    );
  };

  const isHost = protocol === 'host';
  return (
    <Modal title={editing ? `Edit ${connection!.name}` : 'New connection'} onClose={onClose} width={720} className="conn-dialog"
           footer={<>
             {error && <span className="form-error">{error}</span>}
             <button onClick={onClose}>Cancel</button>
             <button className="primary" disabled={!name.trim() || saving} onClick={save}>{saving ? 'Saving…' : 'Save'}</button>
           </>}>
      <div className="placement muted">
        {parentName ? <>Child of <b>{parentName}</b> — inherits hostname and credentials</> : folder ? <>In folder <b>{folder.name}</b></> : 'At the root'}
      </div>
      <div className="tabs-inline" role="tablist">
        {(['general', 'auth', 'settings'] as const).map((t) => (
          <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? 'active' : ''} onClick={() => setTab(t)}>
            {t === 'general' ? 'General' : t === 'auth' ? 'Authentication' : 'Protocol settings'}
          </button>
        ))}
      </div>

      {tab === 'general' && (
        <div className="form-grid">
          <label className="field span2"><span>Display name</span>
            <input value={name} onChange={(e) => setName(e.target.value)} autoFocus placeholder="e.g. Production DB server" />
          </label>
          <label className="field"><span>Protocol</span>
            <select value={protocol} onChange={(e) => setProtocol(e.target.value as Protocol)}>
              {(Object.keys(PROTOCOL_LABELS) as Protocol[]).map((p) => <option key={p} value={p}>{PROTOCOL_LABELS[p]}</option>)}
            </select>
          </label>
          <label className="field"><span>Port</span>
            <input type="number" value={port} onChange={(e) => setPort(e.target.value)} disabled={isHost}
                   placeholder={portDefault ? `${portDefault} (default)` : ''} />
          </label>
          <label className="field span2"><span>Hostname or IP address</span>
            <input value={hostname} onChange={(e) => setHostname(e.target.value)} placeholder={inheritedHost ?? 'server01.example.com'} spellCheck={false} />
            {hostname ? (inheritedHost && <Inherited from={`${inheritedHost} of ${parentName}`} onReset={() => setHostname('')} />)
              : <Inherited value={inheritedHost} from={sourceLabel(inherited?.hostname) || parentName} />}
          </label>
          <label className="field span2"><span>DNS search domain</span>
            <input value={searchDomain} onChange={(e) => setSearchDomain(e.target.value)} placeholder={inheritedSearch ?? 'only applied to single-label host names'} spellCheck={false} />
            {searchDomain ? (inheritedSearch && <Inherited from={inheritedSearch} onReset={() => setSearchDomain('')} />)
              : <Inherited value={inheritedSearch} from={sourceLabel(inherited?.search_domains)} />}
          </label>
          <label className="field span2"><span>Tags (comma separated)</span>
            <input value={tags} onChange={(e) => setTags(e.target.value)} placeholder="prod, linux" />
          </label>
          <label className="field span2"><span>Description</span>
            <textarea rows={3} value={description} onChange={(e) => setDescription(e.target.value)} />
          </label>
          {effective && (
            <div className="span2 effective-box">
              <b>Effective target:</b> {effective.hostname_candidates[0] ?? '—'}:{effective.port?.value ?? '—'}
              {effective.hostname_candidates.length > 1 && <span className="muted"> (tries {effective.hostname_candidates.join(', ')})</span>}
            </div>
          )}
          {protocol === 'ftp' && <div className="span2 banner warn"><ShieldAlert size={14} /> Plain FTP transmits credentials and data unencrypted. Prefer FTPS or SFTP.</div>}
        </div>
      )}

      {tab === 'auth' && (
        <div>
          <div className="radio-cards">
            {AUTH_MODES.map((m) => (
              <label key={m.value} className={`radio-card ${authMode === m.value ? 'active' : ''}`}>
                <input type="radio" name="auth_mode" checked={authMode === m.value} onChange={() => setAuthMode(m.value)} />
                <b>{m.label}</b>
                <small>{m.help}</small>
              </label>
            ))}
          </div>
          {(authMode === 'direct' || authMode === 'interactive') && (
            <div className="form-grid">
              <label className="field"><span>Username{authMode === 'interactive' ? ' (pre-filled)' : ''}</span>
                <input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="off"
                       placeholder={inherited?.auth?.username?.value ?? ''} />
              </label>
              <label className="field"><span>Domain</span>
                <input value={domain} onChange={(e) => setDomain(e.target.value)} placeholder={inherited?.auth?.domain?.value ?? ''} />
              </label>
              {authMode === 'direct' && (
                <label className="field span2"><span>Password</span>
                  {password === null && connection?.password_set ? (
                    <div className="row">
                      <span className="muted">•••••••• (stored, encrypted)</span>
                      <button type="button" onClick={() => setPassword('')}>Change</button>
                      <button type="button" className="danger" onClick={() => setPassword('')}>Clear</button>
                    </div>
                  ) : (
                    <input type="password" value={password ?? ''} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password"
                           placeholder={connection?.password_set ? 'empty = remove stored password' : 'optional'} />
                  )}
                </label>
              )}
            </div>
          )}
          {authMode === 'credential' && (
            <label className="field"><span>Credential entry</span>
              <select value={credentialId} onChange={(e) => setCredentialId(e.target.value)}>
                <option value="">— choose —</option>
                {(creds?.items ?? []).filter((c) => ['connect', 'edit', 'manage'].includes(c.permission ?? '')).map((c) => (
                  <option key={c.id} value={c.id}>{c.name}{c.username ? ` (${c.domain ? `${c.domain}\\` : ''}${c.username})` : ''}</option>
                ))}
              </select>
            </label>
          )}
          <label className="check">
            <input type="checkbox" checked={inheritAuth} onChange={(e) => setInheritAuth(e.target.checked)} />
            <span>Fill missing fields from the parent connection</span>
            <small className="muted">Turn off for fully independent authentication.</small>
          </label>
          {(effective || inherited) && (
            <div className="effective-box">
              <b>{effective ? 'Effective authentication (saved state)' : 'Inherited from parent'}</b>
              <dl className="kv">
                <dt>Username</dt>{authRow('username', 'username')}
                <dt>Password</dt>{authRow('password', 'password')}
                <dt>Domain</dt>{authRow('domain', 'domain')}
                {(effective ?? inherited)?.auth?.private_key && <><dt>Private key</dt><dd>set — from {sourceLabel((effective ?? inherited)!.auth.private_key)}</dd></>}
              </dl>
            </div>
          )}
        </div>
      )}

      {tab === 'settings' && (
        isHost ? <p className="muted">Host entries only carry shared values (hostname, credentials, search domain) for their child connections.</p>
          : groups.map((g) => (
            <fieldset key={g} className="settings-group">
              <legend>{g}</legend>
              <div className="form-grid">{fields.filter((f) => f.group === g).map(renderField)}</div>
            </fieldset>
          ))
      )}
    </Modal>
  );
}
