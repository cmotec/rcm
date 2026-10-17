import { useMemo, useState } from 'react';
import { errorMessage, patch, post } from '../../api/client';
import { folderPath, useCredentialFolders, useCredentials, useInvalidate } from '../../api/hooks';
import type { AuthMode, Folder } from '../../api/types';
import { dialogs } from '../../state/dialogs';
import { FOLDER_COLORS, FOLDER_ICONS } from '../common/icons';
import { Modal } from '../common/Modal';
import { SearchSelect } from '../common/SearchSelect';
import { ACCOUNT_CHOICE_OPTIONS, credentialChoiceFields, credentialChoiceValue, isAccountChoice } from './credentialChoice';

const FOLDER_AUTH_MODES: { value: AuthMode; label: string; help: string }[] = [
  { value: 'inherit', label: 'Inherit', help: 'Use the authentication of the parent folder (if any).' },
  { value: 'direct', label: 'Folder login', help: 'Username, password and domain for the connections in this folder.' },
  { value: 'credential', label: 'Credential store', help: 'A fixed credential entry, or let the user pick one when connecting.' },
  { value: 'interactive', label: 'Ask when connecting', help: 'Prompt for username and password; nothing is stored.' },
];

export function FolderDialog({ folder, parentId, onClose }: { folder?: Folder; parentId?: string | null; onClose: () => void }) {
  const invalidate = useInvalidate();
  const { data: creds } = useCredentials();
  const { data: credFolders } = useCredentialFolders();
  const [name, setName] = useState(folder?.name ?? '');
  const [icon, setIcon] = useState(folder?.icon ?? 'folder');
  const [color, setColor] = useState(folder?.color ?? '');
  const [description, setDescription] = useState(folder?.description ?? '');
  const [searchDomain, setSearchDomain] = useState(folder?.search_domain ?? '');
  const [authMode, setAuthMode] = useState<AuthMode>(folder?.auth_mode ?? 'inherit');
  const [username, setUsername] = useState(folder?.username ?? '');
  const [domain, setDomain] = useState(folder?.domain ?? '');
  // null = keep the stored password; '' with a stored one = remove it
  const [password, setPassword] = useState<string | null>(folder?.password_set ? null : '');
  const [credentialId, setCredentialId] = useState(credentialChoiceValue(folder?.credential_id, folder?.personal_account));
  const [credentialFolderId, setCredentialFolderId] = useState(folder?.credential_folder_id ?? '');
  const [error, setError] = useState<string | null>(null);
  const credentialOptions = useMemo(() => (creds?.items ?? [])
    .filter((c) => ['connect', 'edit', 'manage'].includes(c.permission ?? ''))
    .map((c) => ({ value: c.id, label: c.name, hint: c.username ? `${c.domain ? `${c.domain}\\` : ''}${c.username}` : undefined })),
  [creds]);
  const credentialFolderOptions = useMemo(() => (credFolders ?? [])
    .map((f) => ({ value: f.id, label: folderPath(credFolders ?? [], f.id) }))
    .sort((a, b) => a.label.localeCompare(b.label)), [credFolders]);

  const save = async () => {
    const body: Record<string, unknown> = {
      name: name.trim(),
      icon: icon === 'folder' ? null : icon,
      color: color || null,
      description: description.trim() || null,
      search_domain: searchDomain.trim() || null,
      auth_mode: authMode,
    };
    if (authMode === 'direct' || authMode === 'interactive') {
      body.username = username.trim() || null;
      body.domain = domain.trim() || null;
    }
    if (authMode === 'direct' && password !== null) body.password = password || null;
    if (authMode === 'credential') {
      Object.assign(body, credentialChoiceFields(credentialId));
      body.credential_folder_id = credentialId ? null : credentialFolderId || null;
    }
    try {
      if (folder) await patch(`/api/v1/folders/${folder.id}`, { ...body, version: folder.version });
      else await post('/api/v1/folders', { ...body, parent_id: parentId ?? null });
      invalidate.tree();
      dialogs.toast(folder ? 'Folder saved' : 'Folder created', 'success');
      onClose();
    } catch (err) {
      setError(errorMessage(err));
    }
  };

  return (
    <Modal title={folder ? `Edit folder ${folder.name}` : 'New folder'} onClose={onClose} width={640}
           footer={<>
             {error && <span className="form-error">{error}</span>}
             <button onClick={onClose}>Cancel</button>
             <button className="primary" disabled={!name.trim()} onClick={save}>Save</button>
           </>}>
      <label className="field"><span>Name</span><input value={name} onChange={(e) => setName(e.target.value)} autoFocus /></label>
      <div className="field"><span>Icon</span>
        <div className="icon-picker">
          {Object.entries(FOLDER_ICONS).map(([key, Icon]) => (
            <button key={key} type="button" className={`icon-choice ${icon === key ? 'active' : ''}`} onClick={() => setIcon(key)} title={key}>
              <Icon size={16} style={{ color: color || undefined }} />
            </button>
          ))}
        </div>
      </div>
      <div className="field"><span>Colour</span>
        <div className="color-picker">
          <button type="button" className={`swatch none ${!color ? 'active' : ''}`} onClick={() => setColor('')} title="Default">∅</button>
          {FOLDER_COLORS.map((c) => (
            <button key={c} type="button" className={`swatch ${color === c ? 'active' : ''}`} style={{ background: c }} onClick={() => setColor(c)} aria-label={c} />
          ))}
        </div>
      </div>
      <label className="field"><span>DNS search domain for connections in this folder</span>
        <input value={searchDomain} onChange={(e) => setSearchDomain(e.target.value)} placeholder="optional, e.g. corp.example.com" />
      </label>
      <label className="field"><span>Description</span><textarea rows={2} value={description} onChange={(e) => setDescription(e.target.value)} /></label>

      <fieldset className="settings-group">
        <legend>Authentication</legend>
        <p className="muted small">
          Used by the connections in this folder and its sub-folders whose authentication is set to <b>Inherit</b> (or
          that leave fields empty). A parent connection's login comes first; a connection's own values always win.
        </p>
        <div className="radio-cards">
          {FOLDER_AUTH_MODES.map((m) => (
            <label key={m.value} className={`radio-card ${authMode === m.value ? 'active' : ''}`}>
              <input type="radio" name="folder_auth_mode" checked={authMode === m.value} onChange={() => setAuthMode(m.value)} />
              <b>{m.label}</b>
              <small>{m.help}</small>
            </label>
          ))}
        </div>
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
                {password === null ? (
                  <div className="row">
                    <span className="muted">•••••••• (stored, encrypted)</span>
                    <button type="button" onClick={() => setPassword('')}>Change</button>
                    <button type="button" className="danger" onClick={() => setPassword('')}>Clear</button>
                  </div>
                ) : (
                  <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password"
                         placeholder={folder?.password_set ? 'empty = remove stored password' : 'optional'} />
                )}
              </label>
            )}
          </div>
        )}
        {authMode === 'credential' && (
          <label className="field"><span>Credential entry</span>
            <SearchSelect value={credentialId} onChange={setCredentialId} emptyLabel="Let the user choose when connecting"
                          placeholder="Search credentials…" options={[...ACCOUNT_CHOICE_OPTIONS, ...credentialOptions]} />
            <small className="muted">
              {isAccountChoice(credentialId) ? 'The connecting user’s own account (Profile → My accounts).'
                : credentialId ? 'Pinned: connections in this folder start with this credential.'
                : 'Not pinned: when connecting, users pick from their accounts and the credentials they are allowed to use.'}
            </small>
          </label>
        )}
        {authMode === 'credential' && !credentialId && (
          <label className="field"><span>Offer only credentials from this folder</span>
            <SearchSelect value={credentialFolderId} onChange={setCredentialFolderId}
                          emptyLabel="— all credentials the user may use —" placeholder="Search folders…"
                          options={credentialFolderOptions} />
            <small className="muted">Includes the folders below it.</small>
          </label>
        )}
        {(authMode === 'interactive' || (authMode === 'credential' && !credentialId)) && (
          <small className="muted">Only asked when the connection has no password or key of its own.</small>
        )}
      </fieldset>
    </Modal>
  );
}
