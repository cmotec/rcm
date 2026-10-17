import { useMemo, useState } from 'react';
import { errorMessage, patch } from '../../api/client';
import { folderPath, useCredentialFolders, useCredentials, useInvalidate } from '../../api/hooks';
import type { AuthMode, Connection, LookupField } from '../../api/types';
import { dialogs } from '../../state/dialogs';
import { Modal } from '../common/Modal';
import { SearchSelect } from '../common/SearchSelect';
import { ACCOUNT_CHOICE_OPTIONS, credentialChoiceFields } from './credentialChoice';

const AUTH_LABELS: Record<AuthMode, string> = {
  inherit: 'Inherit (parent connection / folder)',
  direct: 'Connection-specific',
  credential: 'Credential store',
  interactive: 'Ask when connecting',
};
const AUTH_FIELDS: LookupField[] = ['username', 'password', 'domain', 'private_key', 'passphrase'];

type Field = 'auth' | 'inherit' | 'port' | 'domain' | 'description' | 'tags';

/** Change the same parameters on several connections; only the ticked fields are written. */
export function BulkEditDialog({ connections, onClose }: { connections: Connection[]; onClose: () => void }) {
  const invalidate = useInvalidate();
  const { data: creds } = useCredentials();
  const { data: credFolders } = useCredentialFolders();
  const editable = connections.filter((c) => ['edit', 'manage'].includes(c.permission ?? ''));
  const [on, setOn] = useState<Set<Field>>(new Set());
  const [authMode, setAuthMode] = useState<AuthMode>('inherit');
  const [username, setUsername] = useState('');
  const [domain, setDomain] = useState('');
  const [password, setPassword] = useState('');
  const [credentialId, setCredentialId] = useState('');
  const [credentialFolderId, setCredentialFolderId] = useState('');
  const [inheritAuth, setInheritAuth] = useState(true);
  const [port, setPort] = useState('');
  const [searchDomain, setSearchDomain] = useState('');
  const [description, setDescription] = useState('');
  const [tags, setTags] = useState('');
  const [saving, setSaving] = useState(false);

  const credentialOptions = useMemo(() => (creds?.items ?? [])
    .filter((c) => ['connect', 'edit', 'manage'].includes(c.permission ?? ''))
    .map((c) => ({ value: c.id, label: c.name, hint: c.username ? `${c.domain ? `${c.domain}\\` : ''}${c.username}` : undefined })),
  [creds]);
  const credentialFolderOptions = useMemo(() => (credFolders ?? [])
    .map((f) => ({ value: f.id, label: folderPath(credFolders ?? [], f.id) }))
    .sort((a, b) => a.label.localeCompare(b.label)), [credFolders]);

  const toggle = (f: Field, value: boolean) => setOn((s) => {
    const next = new Set(s);
    if (value) next.add(f);
    else next.delete(f);
    return next;
  });
  const portValid = !on.has('port') || port.trim() === '' || (/^\d{1,5}$/.test(port.trim()) && Number(port) <= 65535);

  const apply = async () => {
    setSaving(true);
    const failures: string[] = [];
    let done = 0;
    for (const c of editable) {
      const body: Record<string, unknown> = { version: c.version };
      // a field set here replaces its lookup (a field with a lookup has no own value)
      const dropLookups = new Set<string>();
      if (on.has('auth')) {
        body.auth_mode = authMode;
        if (authMode === 'direct' || authMode === 'interactive') {
          body.username = username.trim() || null;
          body.domain = domain.trim() || null;
        }
        if (authMode === 'direct' && password) body.password = password;
        if (authMode === 'credential') {
          Object.assign(body, credentialChoiceFields(credentialId));
          body.credential_folder_id = credentialId ? null : credentialFolderId || null;
        }
        AUTH_FIELDS.forEach((f) => dropLookups.add(f));
      }
      if (on.has('inherit')) body.inherit_auth = inheritAuth;
      if (on.has('port')) { body.port = port.trim() ? Number(port) : null; dropLookups.add('port'); }
      if (on.has('domain')) { body.search_domain = searchDomain.trim() || null; dropLookups.add('search_domain'); }
      if (on.has('description')) { body.description = description.trim() || null; dropLookups.add('description'); }
      if (on.has('tags')) body.tags = tags.split(',').map((t) => t.trim()).filter(Boolean);
      const lookups = Object.fromEntries(Object.entries(c.lookups ?? {}).filter(([k]) => !dropLookups.has(k)));
      if (Object.keys(lookups).length !== Object.keys(c.lookups ?? {}).length) body.lookups = lookups;
      try {
        await patch(`/api/v1/connections/${c.id}`, body);
        done += 1;
      } catch (err) {
        failures.push(`${c.name}: ${errorMessage(err)}`);
      }
    }
    setSaving(false);
    invalidate.tree();
    if (done) dialogs.toast(`Updated ${done} connection${done === 1 ? '' : 's'}`, 'success');
    if (failures.length) dialogs.toast(`Not updated: ${failures.join('; ')}`, 'error');
    else onClose();
  };

  const check = (f: Field, label: string) => (
    <label className="check bulk-toggle">
      <input type="checkbox" checked={on.has(f)} onChange={(e) => toggle(f, e.target.checked)} />
      <span>{label}</span>
    </label>
  );

  return (
    <Modal title={`Edit ${editable.length} connection${editable.length === 1 ? '' : 's'}`} onClose={onClose} width={640}
           footer={<>
             <button onClick={onClose}>Cancel</button>
             <button className="primary" disabled={!on.size || !editable.length || !portValid || saving} onClick={() => void apply()}>
               {saving ? 'Saving…' : `Apply to ${editable.length}`}</button>
           </>}>
      <p className="muted small">
        Tick the fields to change; the others stay as they are on each connection.
        {connections.length > editable.length && ` ${connections.length - editable.length} selected connection(s) you may not edit are skipped.`}
      </p>

      {check('auth', 'Authentication')}
      {on.has('auth') && (
        <div className="bulk-fields">
          <label className="field"><span>Mode</span>
            <select value={authMode} onChange={(e) => setAuthMode(e.target.value as AuthMode)}>
              {(Object.keys(AUTH_LABELS) as AuthMode[]).map((m) => <option key={m} value={m}>{AUTH_LABELS[m]}</option>)}
            </select>
          </label>
          {(authMode === 'direct' || authMode === 'interactive') && (
            <div className="form-grid">
              <label className="field"><span>Username{authMode === 'interactive' ? ' (pre-filled)' : ''}</span>
                <input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="off" spellCheck={false} />
              </label>
              <label className="field"><span>Domain</span>
                <input value={domain} onChange={(e) => setDomain(e.target.value)} autoComplete="off" spellCheck={false} />
              </label>
              {authMode === 'direct' && (
                <label className="field span2"><span>Password</span>
                  <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password"
                         placeholder="empty = keep each connection's stored password" />
                </label>
              )}
            </div>
          )}
          {authMode === 'credential' && (
            <>
              <label className="field"><span>Credential entry</span>
                <SearchSelect value={credentialId} onChange={setCredentialId} emptyLabel="Let the user choose when connecting"
                              placeholder="Search credentials…" options={[...ACCOUNT_CHOICE_OPTIONS, ...credentialOptions]} />
              </label>
              {!credentialId && (
                <label className="field"><span>Offer only credentials from this folder</span>
                  <SearchSelect value={credentialFolderId} onChange={setCredentialFolderId}
                                emptyLabel="— all credentials the user may use —" placeholder="Search folders…"
                                options={credentialFolderOptions} />
                </label>
              )}
            </>
          )}
        </div>
      )}

      {check('inherit', 'Fill missing login fields from the parent connection or folder')}
      {on.has('inherit') && (
        <div className="bulk-fields">
          <select value={inheritAuth ? 'yes' : 'no'} onChange={(e) => setInheritAuth(e.target.value === 'yes')}>
            <option value="yes">Yes (default)</option>
            <option value="no">No — fully independent authentication</option>
          </select>
        </div>
      )}

      {check('port', 'Port')}
      {on.has('port') && (
        <div className="bulk-fields">
          <input inputMode="numeric" value={port} onChange={(e) => setPort(e.target.value)} placeholder="empty = protocol default" />
          {!portValid && <small className="form-error">1–65535</small>}
        </div>
      )}

      {check('domain', 'Domain (DNS search domain)')}
      {on.has('domain') && (
        <div className="bulk-fields">
          <input value={searchDomain} onChange={(e) => setSearchDomain(e.target.value)} placeholder="empty = inherit" spellCheck={false} />
        </div>
      )}

      {check('description', 'Description')}
      {on.has('description') && (
        <div className="bulk-fields">
          <input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="empty = remove" />
        </div>
      )}

      {check('tags', 'Tags (replace)')}
      {on.has('tags') && (
        <div className="bulk-fields">
          <input value={tags} onChange={(e) => setTags(e.target.value)} placeholder="comma separated; empty = remove all" />
        </div>
      )}

      <details className="bulk-list">
        <summary className="muted small">Connections ({editable.length})</summary>
        <ul>{editable.map((c) => <li key={c.id}>{c.name}{c.hostname ? <span className="muted"> — {c.hostname}</span> : null}</li>)}</ul>
      </details>
    </Modal>
  );
}
