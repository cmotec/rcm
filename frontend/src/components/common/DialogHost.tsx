import { ShieldAlert, TriangleAlert } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { folderPath, folderSubtree, useCredentialFolders, useCredentials } from '../../api/hooks';
import type { Protocol } from '../../api/types';
import { type DialogRequest, type InteractiveCredentials, useDialogStore } from '../../state/dialogs';
import { Modal } from './Modal';

function ConfirmDialog({ req }: { req: Extract<DialogRequest, { kind: 'confirm' }> }) {
  return (
    <Modal title={req.title} onClose={() => req.resolve(false)} width={440}
           footer={<>
             <button onClick={() => req.resolve(false)}>Cancel</button>
             <button className={req.danger ? 'danger primary' : 'primary'} onClick={() => req.resolve(true)}>
               {req.confirmLabel ?? 'OK'}
             </button>
           </>}>
      <p>{req.message}</p>
    </Modal>
  );
}

function PromptDialog({ req }: { req: Extract<DialogRequest, { kind: 'prompt' }> }) {
  const [value, setValue] = useState(req.initial ?? '');
  return (
    <Modal title={req.title} onClose={() => req.resolve(null)} width={440}
           footer={<>
             <button onClick={() => req.resolve(null)}>Cancel</button>
             <button className="primary" onClick={() => req.resolve(value.trim() || null)}>OK</button>
           </>}>
      <form onSubmit={(e) => { e.preventDefault(); req.resolve(value.trim() || null); }}>
        <label className="field">
          <span>{req.label}</span>
          <input type={req.secret ? 'password' : 'text'} value={value} onChange={(e) => setValue(e.target.value)} autoFocus />
        </label>
      </form>
    </Modal>
  );
}

function CredentialsDialog({ req }: { req: Extract<DialogRequest, { kind: 'credentials' }> }) {
  const [v, setV] = useState<InteractiveCredentials>({ username: req.username ?? '', password: '', domain: req.domain ?? '' });
  const submit = () => {
    const out: InteractiveCredentials = {};
    if (v.username) out.username = v.username;
    if (v.password) out.password = v.password;
    if (v.domain) out.domain = v.domain;
    req.resolve(out);
  };
  return (
    <Modal title={`Sign in to ${req.connectionName}`} onClose={() => req.resolve(null)} width={420}
           footer={<>
             <button onClick={() => req.resolve(null)}>Cancel</button>
             <button className="primary" onClick={submit}>Connect</button>
           </>}>
      <form onSubmit={(e) => { e.preventDefault(); submit(); }} autoComplete="off">
        <p className="muted">These credentials are used for this session only and are not stored.</p>
        <label className="field"><span>Username</span>
          <input value={v.username} onChange={(e) => setV({ ...v, username: e.target.value })} autoFocus={!req.username} />
        </label>
        <label className="field"><span>Password</span>
          <input type="password" value={v.password} onChange={(e) => setV({ ...v, password: e.target.value })}
                 autoFocus={Boolean(req.username)} />
        </label>
        {req.fields.includes('domain') && (
          <label className="field"><span>Domain</span>
            <input value={v.domain} onChange={(e) => setV({ ...v, domain: e.target.value })} />
          </label>
        )}
        <button type="submit" hidden />
      </form>
    </Modal>
  );
}

function HostKeyDialog({ req }: { req: Extract<DialogRequest, { kind: 'hostkey' }> }) {
  const { info, changed } = req;
  const blocked = changed && !info.can_replace;
  return (
    <Modal title={changed ? 'SSH host key has changed' : 'Unknown SSH host key'} onClose={() => req.resolve(false)}
           width={560}
           footer={<>
             <button onClick={() => req.resolve(false)}>{blocked ? 'Close' : 'Cancel'}</button>
             {!blocked && (
               <button className={changed ? 'danger primary' : 'primary'} onClick={() => req.resolve(true)}>
                 {changed ? 'Replace key and connect' : 'Trust and connect'}
               </button>
             )}
           </>}>
      <div className={`hostkey ${changed ? 'changed' : ''}`}>
        {changed ? <ShieldAlert size={28} /> : <TriangleAlert size={28} />}
        <div>
          {changed ? (
            <p><strong>Warning:</strong> the server presented a different host key than the one trusted before.
              This can mean the server was reinstalled, or that someone is intercepting the connection.
              {blocked && ' Only users who can edit this connection or manage known hosts may replace the key.'}</p>
          ) : (
            <p>The authenticity of <strong>{info.hostname}:{info.port}</strong> cannot be established yet.
              Compare the fingerprint with the one published by the server administrator.</p>
          )}
          <dl className="kv">
            <dt>Connection</dt><dd>{info.connection_name}</dd>
            <dt>Key type</dt><dd>{info.key_type}</dd>
            <dt>Fingerprint</dt><dd className="mono">{info.fingerprint}</dd>
            {info.previous_fingerprint && <><dt>Previously</dt><dd className="mono">{info.previous_fingerprint}</dd></>}
          </dl>
        </div>
      </div>
    </Modal>
  );
}

/** Username / password (/ domain for RDP) typed for one session; not stored. */
function OwnCredentialsForm({ protocol, value, onChange, onSubmit }: {
  protocol: Protocol; value: InteractiveCredentials; onChange: (v: InteractiveCredentials) => void; onSubmit: () => void;
}) {
  return (
    <form onSubmit={(e) => { e.preventDefault(); onSubmit(); }} autoComplete="off">
      <label className="field"><span>Username</span>
        <input value={value.username ?? ''} onChange={(e) => onChange({ ...value, username: e.target.value })} autoFocus />
      </label>
      <label className="field"><span>Password</span>
        <input type="password" value={value.password ?? ''} onChange={(e) => onChange({ ...value, password: e.target.value })}
               autoComplete="new-password" />
      </label>
      {protocol === 'rdp' && (
        <label className="field"><span>Domain</span>
          <input value={value.domain ?? ''} onChange={(e) => onChange({ ...value, domain: e.target.value })} />
        </label>
      )}
      <button type="submit" hidden />
    </form>
  );
}

/** Only the fields that were filled in. */
function typedCredentials(v: InteractiveCredentials): InteractiveCredentials {
  const out: InteractiveCredentials = {};
  if (v.username) out.username = v.username;
  if (v.password) out.password = v.password;
  if (v.domain) out.domain = v.domain;
  return out;
}

function ChooseCredentialDialog({ req }: { req: Extract<DialogRequest, { kind: 'choose-credential' }> }) {
  const { data, isLoading } = useCredentials();
  const { data: folders } = useCredentialFolders();
  const [filter, setFilter] = useState('');
  // only credentials the user may use (connect or higher), from the connection's folder if it has one
  const allowedFolders = req.folderId ? folderSubtree(folders ?? [], req.folderId) : null;
  const usable = (data?.items ?? []).filter((c) => ['connect', 'edit', 'manage'].includes(c.permission ?? ''))
    .filter((c) => !allowedFolders || Boolean(c.folder_id && allowedFolders.has(c.folder_id)));
  const shown = usable.filter((c) => !filter || `${c.name} ${c.username ?? ''} ${c.domain ?? ''}`.toLowerCase().includes(filter.toLowerCase()));
  const [selected, setSelected] = useState<string | null>(null);
  const [tab, setTab] = useState<'store' | 'own' | null>(null);
  const [own, setOwn] = useState<InteractiveCredentials>({ username: '', password: '', domain: '' });
  // nothing to choose from: start on "Enter credentials"
  const active = tab ?? (!isLoading && !usable.length ? 'own' : 'store');
  const valid = active === 'store' ? Boolean(selected) : Boolean(own.username || own.password);
  const submit = () => {
    if (!valid) return;
    if (active === 'store') req.resolve({ mode: 'credential', credential_id: selected! });
    else req.resolve({ mode: 'own', credentials: typedCredentials(own) });
  };
  return (
    <Modal title={`Connect to ${req.connectionName}`} onClose={() => req.resolve(null)} width={480}
           footer={<>
             <button onClick={() => req.resolve(null)}>Cancel</button>
             <button className="primary" disabled={!valid} onClick={submit}>Connect</button>
           </>}>
      <div className="tabs-inline" role="tablist">
        <button role="tab" aria-selected={active === 'store'} className={active === 'store' ? 'active' : ''}
                onClick={() => setTab('store')}>Credential store</button>
        <button role="tab" aria-selected={active === 'own'} className={active === 'own' ? 'active' : ''}
                onClick={() => setTab('own')}>Enter credentials</button>
      </div>
      {active === 'store' ? (
        <>
          <p className="muted">Choose the credential to use for this session
            {req.folderId ? <> (from <b>{folderPath(folders ?? [], req.folderId) || 'its folder'}</b>)</> : null}.</p>
          {usable.length > 6 && (
            <input placeholder="Filter…" value={filter} onChange={(e) => setFilter(e.target.value)} autoFocus />
          )}
          {isLoading && <p className="muted">Loading…</p>}
          {!isLoading && !usable.length && <p className="banner warn">You have no credentials you are allowed to use.</p>}
          <ul className="choice-list" role="listbox">
            {shown.map((c) => (
              <li key={c.id} role="option" aria-selected={selected === c.id} className={selected === c.id ? 'active' : ''}
                  onClick={() => setSelected(c.id)} onDoubleClick={() => req.resolve({ mode: 'credential', credential_id: c.id })}>
                <b>{c.name}</b>
                <span className="muted">{c.domain ? `${c.domain}\\` : ''}{c.username ?? ''}</span>
              </li>
            ))}
          </ul>
        </>
      ) : (
        <>
          <p className="muted">Used for this session only, not stored.</p>
          <OwnCredentialsForm protocol={req.protocol} value={own} onChange={setOwn} onSubmit={submit} />
        </>
      )}
    </Modal>
  );
}

function OpenWithDialog({ req }: { req: Extract<DialogRequest, { kind: 'open-with' }> }) {
  const { data } = useCredentials();
  const usable = (data?.items ?? []).filter((c) => ['connect', 'edit', 'manage'].includes(c.permission ?? ''));
  const [mode, setMode] = useState<'own' | 'credential'>('own');
  const [v, setV] = useState<InteractiveCredentials>({ username: '', password: '', domain: '' });
  const [credId, setCredId] = useState<string | null>(null);
  const valid = mode === 'own' ? Boolean(v.username || v.password) : Boolean(credId);
  const submit = () => {
    if (!valid) return;
    if (mode === 'credential') {
      req.resolve({ mode: 'credential', credential_id: credId! });
      return;
    }
    req.resolve({ mode: 'own', credentials: typedCredentials(v) });
  };
  return (
    <Modal title={`Open ${req.connectionName} with different credentials`} onClose={() => req.resolve(null)} width={480}
           footer={<>
             <button onClick={() => req.resolve(null)}>Cancel</button>
             <button className="primary" disabled={!valid} onClick={submit}>Connect</button>
           </>}>
      <p className="muted">Used for this session only, instead of the connection's configured login.</p>
      <div className="radio-cards two">
        <label className={`radio-card ${mode === 'own' ? 'active' : ''}`}>
          <input type="radio" name="open-with" checked={mode === 'own'} onChange={() => setMode('own')} />
          <b>Enter credentials</b><small>Typed now, not stored.</small>
        </label>
        <label className={`radio-card ${mode === 'credential' ? 'active' : ''}`}>
          <input type="radio" name="open-with" checked={mode === 'credential'} onChange={() => setMode('credential')} />
          <b>Credential store</b><small>Any entry you are allowed to use.</small>
        </label>
      </div>
      {mode === 'own' ? (
        <OwnCredentialsForm protocol={req.protocol} value={v} onChange={setV} onSubmit={submit} />
      ) : (
        <>
          {!usable.length && <p className="banner warn">You have no credentials you are allowed to use.</p>}
          <ul className="choice-list" role="listbox">
            {usable.map((c) => (
              <li key={c.id} role="option" aria-selected={credId === c.id} className={credId === c.id ? 'active' : ''}
                  onClick={() => setCredId(c.id)} onDoubleClick={() => req.resolve({ mode: 'credential', credential_id: c.id })}>
                <b>{c.name}</b>
                <span className="muted">{c.domain ? `${c.domain}\\` : ''}{c.username ?? ''}</span>
              </li>
            ))}
          </ul>
        </>
      )}
    </Modal>
  );
}

/** Taps right after the menu opened belong to the gesture that opened it (touch long press). */
const MENU_ARM_MS = 400;

function ContextMenu() {
  const menu = useDialogStore((s) => s.menu);
  const openedAt = useRef(0);
  useEffect(() => {
    if (!menu) return;
    openedAt.current = Date.now();
    const close = () => useDialogStore.setState({ menu: null });
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && close();
    const onPointer = () => Date.now() - openedAt.current >= MENU_ARM_MS && close();
    window.addEventListener('mousedown', onPointer);
    window.addEventListener('blur', close);
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('mousedown', onPointer);
      window.removeEventListener('blur', close);
      window.removeEventListener('keydown', onKey);
    };
  }, [menu]);
  if (!menu) return null;
  const x = Math.min(menu.x, window.innerWidth - 230);
  const y = Math.min(menu.y, window.innerHeight - menu.items.length * 30 - 16);
  return (
    <ul className="context-menu" style={{ left: x, top: Math.max(4, y) }} role="menu"
        onMouseDown={(e) => e.stopPropagation()}>
      {menu.items.map((item, i) =>
        item.separator ? <li key={i} className="sep" role="separator" /> : (
          <li key={i} role="menuitem" className={`${item.danger ? 'danger' : ''} ${item.disabled ? 'disabled' : ''}`}
              onClick={() => {
                if (item.disabled || Date.now() - openedAt.current < MENU_ARM_MS) return;
                useDialogStore.setState({ menu: null });
                item.action?.();
              }}>
            {item.icon}<span>{item.label}</span>
          </li>
        ))}
    </ul>
  );
}

function Toasts() {
  const toasts = useDialogStore((s) => s.toasts);
  return (
    <div className="toasts" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.id} className={`toast ${t.kind}`}
             onClick={() => useDialogStore.setState((s) => ({ toasts: s.toasts.filter((x) => x.id !== t.id) }))}>
          {t.message}
        </div>
      ))}
    </div>
  );
}

export function DialogHost() {
  const queue = useDialogStore((s) => s.queue);
  const req = queue[0];
  return (
    <>
      {req?.kind === 'confirm' && <ConfirmDialog key={req.id} req={req} />}
      {req?.kind === 'prompt' && <PromptDialog key={req.id} req={req} />}
      {req?.kind === 'credentials' && <CredentialsDialog key={req.id} req={req} />}
      {req?.kind === 'hostkey' && <HostKeyDialog key={req.id} req={req} />}
      {req?.kind === 'choose-credential' && <ChooseCredentialDialog key={req.id} req={req} />}
      {req?.kind === 'open-with' && <OpenWithDialog key={req.id} req={req} />}
      <ContextMenu />
      <Toasts />
    </>
  );
}
