import { CornerDownRight, RotateCcw, ShieldAlert } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { errorMessage, get, patch, post } from '../../api/client';
import { folderPath, useCredentialFolders, useCredentials, useInvalidate, useTree } from '../../api/hooks';
import type { AuthMode, Connection, EffectiveConnection, LookupField, Protocol, ResolvedValue } from '../../api/types';
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
  { value: 'inherit', label: 'Inherit', help: 'Use the credentials of the parent connection, or of the folder.' },
  { value: 'direct', label: 'Connection-specific', help: 'Username, password and domain stored on this connection.' },
  { value: 'credential', label: 'Credential store', help: 'A fixed entry from the credential store, or let the user pick one when connecting.' },
  { value: 'interactive', label: 'Ask when connecting', help: 'Prompt for username and password; nothing is stored.' },
];

/** A whole field value of the form {{lookup:<credential name pattern>:<field>}}. */
const LOOKUP_RE = /^\s*\{\{\s*lookup\s*:.+:[A-Za-z][A-Za-z0-9_]*\s*\}\}\s*$/;
export const isLookup = (v: string | null | undefined): boolean => Boolean(v && LOOKUP_RE.test(v));

function LookupHint() {
  return (
    <details className="lookup-hint span2">
      <summary>Take values from the credential store (lookups)</summary>
      <p className="muted small">
        Instead of a value, a field can hold <code>{'{{lookup:<credential name>:<field>}}'}</code>. When the connection is
        opened, the credential with that name is looked up and its field is used. In the name, <code>{'{hostname}'}</code>,
        {' '}<code>{'{name}'}</code> and <code>{'{domain}'}</code> stand for this connection's host name, name and domain, and
        {' '}<code>*</code> matches any text. Fields: <code>username</code>, <code>password</code>, <code>domain</code>,
        {' '}<code>private_key</code>, <code>passphrase</code>, or any attribute of the credential (e.g. <code>port</code>,
        {' '}<code>description</code>). Exactly one credential you may use must match. Example:
        {' '}<code>{'{{lookup:{hostname}_root:username}}'}</code>
      </p>
    </details>
  );
}

function sourceLabel(r: ResolvedValue | null | undefined): string {
  if (!r) return '';
  const s = r.source;
  if (s.kind === 'default') return 'protocol default';
  if (s.kind === 'lookup') return `lookup → credential “${s.name ?? s.id}”`;
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
  const { data: credFolders } = useCredentialFolders();
  const invalidate = useInvalidate();
  const lk = connection?.lookups ?? {};
  const parentId = connection?.parent_id ?? placement?.parentId ?? null;
  const folderId = connection?.folder_id ?? placement?.folderId ?? null;
  const parent = tree?.connections.find((c) => c.id === parentId);
  const folder = tree?.folders.find((f) => f.id === folderId);

  const [tab, setTab] = useState<'general' | 'auth' | 'settings'>('general');
  const [name, setName] = useState(connection?.name ?? '');
  const [protocol, setProtocol] = useState<Protocol>(connection?.protocol ?? (parent ? 'ssh' : 'ssh'));
  const [hostname, setHostname] = useState(lk.hostname ?? connection?.hostname ?? '');
  const [port, setPort] = useState(lk.port ?? (connection?.port ? String(connection.port) : ''));
  const [searchDomain, setSearchDomain] = useState(lk.search_domain ?? connection?.search_domain ?? '');
  const [description, setDescription] = useState(lk.description ?? connection?.description ?? '');
  const [tags, setTags] = useState((connection?.tags ?? []).join(', '));
  const [authMode, setAuthMode] = useState<AuthMode>(connection?.auth_mode ?? 'inherit');
  const [inheritAuth, setInheritAuth] = useState(connection?.inherit_auth ?? true);
  const [username, setUsername] = useState(lk.username ?? connection?.username ?? '');
  const [domain, setDomain] = useState(lk.domain ?? connection?.domain ?? '');
  const [password, setPassword] = useState<string | null>(lk.password ?? null); // null = unchanged
  const [credentialId, setCredentialId] = useState(connection?.credential_id ?? '');
  const [credentialFolderId, setCredentialFolderId] = useState(connection?.credential_folder_id ?? '');
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
    // a field holding {{lookup:…}} is stored as a lookup, not as its value
    const lookups: Partial<Record<LookupField, string>> = {};
    const plain = (field: LookupField, value: string): string | null => {
      if (isLookup(value)) {
        lookups[field] = value.trim();
        return null;
      }
      return value.trim() || null;
    };
    const portValue = plain('port', port);
    const body: Record<string, unknown> = {
      name: effectiveName,
      hostname: plain('hostname', hostname),
      port: portValue ? Number(portValue) : null,
      search_domain: plain('search_domain', searchDomain),
      description: plain('description', description),
      tags: tags.split(',').map((t) => t.trim()).filter(Boolean),
      auth_mode: authMode,
      inherit_auth: inheritAuth,
      settings: cleanSettings(protocol, settings),
    };
    if (authMode === 'direct' || authMode === 'interactive') {
      body.username = plain('username', username);
      body.domain = plain('domain', domain);
    }
    if (authMode === 'direct' && password !== null) {
      if (isLookup(password)) lookups.password = password.trim();
      else body.password = password || null;
    }
    if (authMode === 'credential') {
      body.credential_id = credentialId || null;
      body.credential_folder_id = credentialId ? null : credentialFolderId || null;
    }
    body.lookups = lookups;
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
  // The name is optional: fall back to the host name, then the description (not lookups).
  const literalHost = isLookup(hostname) ? '' : hostname.trim();
  const effectiveName = name.trim() || literalHost || (isLookup(description) ? '' : description.split(/\r?\n/)[0].trim().slice(0, 255));
  const hostForPreview = (isLookup(hostname) ? effective?.hostname?.value ?? '' : hostname.trim()) || inheritedHost || '';
  const lookupErrors = Object.entries(effective?.lookup_errors ?? {});
  const domainForPreview = (searchDomain.trim() || inheritedSearch?.split(',')[0]?.trim() || '').replace(/^\.+|\.+$/g, '');
  const connectsTo = hostForPreview
    ? (domainForPreview && !hostForPreview.includes('.') && !hostForPreview.includes(':') ? `${hostForPreview}.${domainForPreview}` : hostForPreview)
    : null;
  return (
    <Modal title={editing ? `Edit ${connection!.name}` : 'New connection'} onClose={onClose} width={720} className="conn-dialog"
           footer={<>
             {error && <span className="form-error">{error}</span>}
             <button onClick={onClose}>Cancel</button>
             <button className="primary" disabled={!effectiveName || saving} onClick={save}>{saving ? 'Saving…' : 'Save'}</button>
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
          <label className="field"><span>Hostname or IP address</span>
            <input value={hostname} onChange={(e) => setHostname(e.target.value)} placeholder={inheritedHost ?? 'srv01'}
                   spellCheck={false} autoFocus />
            {hostname ? (inheritedHost && <Inherited from={`${inheritedHost} of ${parentName}`} onReset={() => setHostname('')} />)
              : <Inherited value={inheritedHost} from={sourceLabel(inherited?.hostname) || parentName} />}
          </label>
          <label className="field"><span>Domain (optional)</span>
            <input value={searchDomain} onChange={(e) => setSearchDomain(e.target.value)} placeholder={inheritedSearch ?? 'corp.example.com'} spellCheck={false} />
            {searchDomain ? (inheritedSearch && <Inherited from={inheritedSearch} onReset={() => setSearchDomain('')} />)
              : <Inherited value={inheritedSearch} from={sourceLabel(inherited?.search_domains)} />}
          </label>
          {connectsTo && (
            <div className="span2 muted small connects-to">
              Connects to <b>{connectsTo}</b>. The domain is appended to host names without a dot and is not shown in the connection list.
            </div>
          )}
          <label className="field span2"><span>Description (shown in brackets next to the host name)</span>
            <input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="e.g. Production database" />
          </label>
          <label className="field"><span>Protocol</span>
            <select value={protocol} onChange={(e) => setProtocol(e.target.value as Protocol)}>
              {(Object.keys(PROTOCOL_LABELS) as Protocol[]).map((p) => <option key={p} value={p}>{PROTOCOL_LABELS[p]}</option>)}
            </select>
          </label>
          <label className="field"><span>Port</span>
            <input inputMode="numeric" value={port} onChange={(e) => setPort(e.target.value)} disabled={isHost}
                   placeholder={portDefault ? `${portDefault} (default)` : ''} spellCheck={false} />
          </label>
          <label className="field"><span>Name (optional)</span>
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder={hostname.trim() || 'used when there is no own host name'} />
          </label>
          <label className="field"><span>Tags (comma separated)</span>
            <input value={tags} onChange={(e) => setTags(e.target.value)} placeholder="prod, linux" />
          </label>
          {effective && (
            <div className="span2 effective-box">
              <b>Effective target:</b> {effective.hostname_candidates[0] ?? '—'}:{effective.port?.value ?? '—'}
              {effective.hostname_candidates.length > 1 && <span className="muted"> (tries {effective.hostname_candidates.join(', ')})</span>}
            </div>
          )}
          {protocol === 'ftp' && <div className="span2 banner warn"><ShieldAlert size={14} /> Plain FTP transmits credentials and data unencrypted. Prefer FTPS or SFTP.</div>}
          {lookupErrors.length > 0 && (
            <div className="span2 banner error">
              Lookups that failed (saved state):
              <ul>{lookupErrors.map(([f, msg]) => <li key={f}><b>{f}</b>: {msg}</li>)}</ul>
            </div>
          )}
          <LookupHint />
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
                  {password !== null && isLookup(password) ? (
                    <input value={password} onChange={(e) => setPassword(e.target.value)} spellCheck={false} />
                  ) : password === null && connection?.password_set ? (
                    <div className="row">
                      <span className="muted">•••••••• (stored, encrypted)</span>
                      <button type="button" onClick={() => setPassword('')}>Change</button>
                      <button type="button" className="danger" onClick={() => setPassword('')}>Clear</button>
                    </div>
                  ) : (
                    <input type={password?.startsWith('{{') ? 'text' : 'password'} value={password ?? ''}
                           onChange={(e) => setPassword(e.target.value)} autoComplete="new-password"
                           placeholder={connection?.password_set ? 'empty = remove stored password' : 'optional, or {{lookup:…:password}}'} />
                  )}
                </label>
              )}
              <LookupHint />
            </div>
          )}
          {authMode === 'credential' && (
            <label className="field"><span>Credential entry</span>
              <small className="muted">
                {credentialId ? 'Pinned: the connection always starts with this credential.'
                  : 'Not pinned: when connecting, users pick from the credentials they are allowed to use.'}
              </small>
              <select value={credentialId} onChange={(e) => setCredentialId(e.target.value)}>
                <option value="">Let the user choose when connecting</option>
                {(creds?.items ?? []).filter((c) => ['connect', 'edit', 'manage'].includes(c.permission ?? '')).map((c) => (
                  <option key={c.id} value={c.id}>{c.name}{c.username ? ` (${c.domain ? `${c.domain}\\` : ''}${c.username})` : ''}</option>
                ))}
              </select>
            </label>
          )}
          {authMode === 'credential' && !credentialId && (
            <label className="field"><span>Offer only credentials from this folder</span>
              <select value={credentialFolderId} onChange={(e) => setCredentialFolderId(e.target.value)}>
                <option value="">— all credentials the user may use —</option>
                {[...(credFolders ?? [])]
                  .sort((a, b) => folderPath(credFolders ?? [], a.id).localeCompare(folderPath(credFolders ?? [], b.id)))
                  .map((f) => <option key={f.id} value={f.id}>{folderPath(credFolders ?? [], f.id)}</option>)}
              </select>
              <small className="muted">Includes the folders below it.</small>
            </label>
          )}
          <label className="check">
            <input type="checkbox" checked={inheritAuth} onChange={(e) => setInheritAuth(e.target.checked)} />
            <span>Fill missing fields from the parent connection or folder</span>
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
