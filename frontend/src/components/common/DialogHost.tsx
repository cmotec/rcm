import { ShieldAlert, TriangleAlert } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { folderPath, folderSubtree, useCredentialFolders, useCredentials, useEffective, usePersonalAccounts } from '../../api/hooks';
import type { Credential, PersonalAccount, PersonalAccountKind, Protocol } from '../../api/types';
import { type AuthOverride, type DialogRequest, type InteractiveCredentials, type LoginBatch, useDialogStore } from '../../state/dialogs';
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

/** Several connections opened together: the login chosen here can be used for all of them. */
function ShareLoginCheck({ batch, checked, onChange }: {
  batch?: LoginBatch; checked: boolean; onChange: (v: boolean) => void;
}) {
  if (!batch || batch.count < 2) return null;
  return (
    <label className="check share-login">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <span>Use this login for all {batch.count} connections</span>
    </label>
  );
}

function CredentialsDialog({ req }: { req: Extract<DialogRequest, { kind: 'credentials' }> }) {
  const [v, setV] = useState<InteractiveCredentials>({ username: req.username ?? '', password: '', domain: req.domain ?? '' });
  const accounts = useConfiguredAccounts();
  const [shareAll, setShareAll] = useState(true);
  const finish = (answer: InteractiveCredentials | { mode: 'account'; account: PersonalAccountKind }) => {
    if (shareAll && req.batch) req.batch.shared = 'mode' in answer ? answer : { mode: 'own', credentials: answer };
    req.resolve(answer);
  };
  const submit = () => {
    const out: InteractiveCredentials = {};
    if (v.username) out.username = v.username;
    if (v.password) out.password = v.password;
    if (v.domain) out.domain = v.domain;
    finish(out);
  };
  return (
    <Modal title={`Sign in to ${req.connectionName}`} onClose={() => req.resolve(null)} width={420}
           footer={<>
             <ShareLoginCheck batch={req.batch} checked={shareAll} onChange={setShareAll} />
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
      {accounts.length > 0 && (
        <div className="account-shortcuts">
          <span className="muted">Or sign in with</span>
          {accounts.map((a) => (
            <button key={a.kind} type="button" onClick={() => finish({ mode: 'account', account: a.kind })}
                    title={accountLogin(a)}>
              {ACCOUNT_LABELS[a.kind]}
            </button>
          ))}
        </div>
      )}
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

const ACCOUNT_LABELS: Record<PersonalAccountKind, string> = {
  user: 'My user account',
  privileged: 'My privileged account',
};

/** domain\user, or what is set of it. */
function accountLogin(a: PersonalAccount): string {
  return `${a.domain ? `${a.domain}\\` : ''}${a.username ?? ''}` || '(password only)';
}

/** The user's personal accounts that have something stored (Profile → My accounts). */
function useConfiguredAccounts(): PersonalAccount[] {
  const { data } = usePersonalAccounts();
  return (data ?? []).filter((a) => a.username || a.domain || a.has_password);
}

/** The connection's host name (full and short) and description, lower case: credentials whose name or
 * description contain one of them are listed as matching (e.g. host atplct0005 → credential atplct0005-root). */
function useMatchTerms(connectionId: string): string[] {
  const { data } = useEffective(connectionId);
  const host = typeof data?.hostname?.value === 'string' ? data.hostname.value.trim().toLowerCase() : '';
  const desc = typeof data?.description?.value === 'string' ? data.description.value.split(/\r?\n/)[0].trim().toLowerCase() : '';
  const terms = new Set<string>();
  if (host) {
    terms.add(host);
    if (!/^[\d.]+$/.test(host) && !host.includes(':')) terms.add(host.split('.')[0]); // not for IP addresses
  }
  if (desc) terms.add(desc);
  return [...terms].filter((t) => t.length >= 3);
}

const credentialMatches = (c: Credential, terms: string[]) =>
  terms.some((t) => `${c.name} ${c.description ?? ''}`.toLowerCase().includes(t));

/** A choice in the login list: "account:user" / "account:privileged" or "credential:<id>". */
type LoginKey = string;

function loginOverride(key: LoginKey): AuthOverride {
  const [type, value] = key.split(':');
  return type === 'account' ? { mode: 'account', account: value as PersonalAccountKind } : { mode: 'credential', credential_id: value };
}

/**
 * One list, three sections: the user's own accounts, the credential store (the connection's credential
 * folder, if it has one) and the credentials matching the connection's host (from every folder the user
 * may use). Only credentials the user may connect with are listed.
 */
function useLoginChoices(connectionId: string, folderId: string | null, accountsOnly: boolean, filter: string) {
  const { data, isLoading } = useCredentials();
  const { data: folders } = useCredentialFolders();
  const { isLoading: accountsLoading } = usePersonalAccounts();
  const accounts = useConfiguredAccounts();
  const terms = useMatchTerms(connectionId);
  const q = filter.trim().toLowerCase();
  const hit = (text: string) => !q || text.toLowerCase().includes(q);
  const usable = accountsOnly ? [] : (data?.items ?? []).filter((c) => ['connect', 'edit', 'manage'].includes(c.permission ?? ''));
  const allowedFolders = folderId ? folderSubtree(folders ?? [], folderId) : null;
  const matching = usable.filter((c) => credentialMatches(c, terms));
  const matchingIds = new Set(matching.map((c) => c.id));
  const store = usable.filter((c) => !matchingIds.has(c.id))
    .filter((c) => !allowedFolders || Boolean(c.folder_id && allowedFolders.has(c.folder_id)));
  const credText = (c: Credential) => `${c.name} ${c.username ?? ''} ${c.domain ?? ''}`;
  return {
    loading: isLoading || accountsLoading,
    total: accounts.length + store.length + matching.length,
    accounts: accounts.filter((a) => hit(`${ACCOUNT_LABELS[a.kind]} ${accountLogin(a)}`)),
    store: store.filter((c) => hit(credText(c))),
    matching: matching.filter((c) => hit(credText(c))),
    folderLabel: folderId ? folderPath(folders ?? [], folderId) || 'its folder' : null,
  };
}

function LoginChoiceList({ choices, selected, onSelect, onPick }: {
  choices: ReturnType<typeof useLoginChoices>; selected: LoginKey | null;
  onSelect: (k: LoginKey) => void; onPick: (k: LoginKey) => void;
}) {
  const row = (key: LoginKey, title: string, detail: string) => (
    <li key={key} role="option" aria-selected={selected === key} className={selected === key ? 'active' : ''}
        onClick={() => onSelect(key)} onDoubleClick={() => onPick(key)}>
      <b>{title}</b><span className="muted">{detail}</span>
    </li>
  );
  const credRow = (c: Credential) => row(`credential:${c.id}`, c.name, `${c.domain ? `${c.domain}\\` : ''}${c.username ?? ''}`);
  return (
    <>
      {choices.accounts.length > 0 && (
        <>
          <div className="choice-heading">My accounts</div>
          <ul className="choice-list" role="listbox">
            {choices.accounts.map((a) => row(`account:${a.kind}`, ACCOUNT_LABELS[a.kind],
              `${accountLogin(a)}${a.has_password ? '' : ' (no password)'}`))}
          </ul>
        </>
      )}
      {choices.store.length > 0 && (
        <>
          <div className="choice-heading">Credential store{choices.folderLabel ? ` — ${choices.folderLabel}` : ''}</div>
          <ul className="choice-list" role="listbox">{choices.store.map(credRow)}</ul>
        </>
      )}
      {choices.matching.length > 0 && (
        <>
          <div className="choice-heading">Matching this connection</div>
          <ul className="choice-list" role="listbox">{choices.matching.map(credRow)}</ul>
        </>
      )}
    </>
  );
}

function ChooseCredentialDialog({ req }: { req: Extract<DialogRequest, { kind: 'choose-credential' }> }) {
  const [filter, setFilter] = useState('');
  const accountsOnly = Boolean(req.accountsOnly);
  const choices = useLoginChoices(req.connectionId, req.folderId ?? null, accountsOnly, filter);
  const [picked, setPicked] = useState<LoginKey | null>(null);
  const only = choices.total === 1 ? (choices.accounts[0] ? `account:${choices.accounts[0].kind}`
    : `credential:${(choices.store[0] ?? choices.matching[0])?.id}`) : null;
  const selected = picked ?? only;
  const [tab, setTab] = useState<'choose' | 'own' | null>(null);
  const [own, setOwn] = useState<InteractiveCredentials>({ username: '', password: '', domain: '' });
  // nothing to choose from: start on "Enter credentials"
  const active = accountsOnly ? 'choose' : tab ?? (choices.loading || choices.total ? 'choose' : 'own');
  const valid = active === 'choose' ? Boolean(selected) : Boolean(own.username || own.password);
  const [shareAll, setShareAll] = useState(true);
  const finish = (answer: AuthOverride) => {
    if (shareAll && req.batch) req.batch.shared = answer;
    req.resolve(answer);
  };
  const submit = () => {
    if (!valid) return;
    if (active === 'choose') finish(loginOverride(selected!));
    else finish({ mode: 'own', credentials: typedCredentials(own) });
  };
  return (
    <Modal title={`Connect to ${req.connectionName}`} onClose={() => req.resolve(null)} width={480}
           footer={<>
             <ShareLoginCheck batch={req.batch} checked={shareAll} onChange={setShareAll} />
             <button onClick={() => req.resolve(null)}>Cancel</button>
             <button className="primary" disabled={!valid} onClick={submit}>Connect</button>
           </>}>
      {!accountsOnly && (
        <div className="tabs-inline" role="tablist">
          <button role="tab" aria-selected={active === 'choose'} className={active === 'choose' ? 'active' : ''}
                  onClick={() => setTab('choose')}>Accounts &amp; credentials</button>
          <button role="tab" aria-selected={active === 'own'} className={active === 'own' ? 'active' : ''}
                  onClick={() => setTab('own')}>Enter credentials</button>
        </div>
      )}
      {active === 'choose' ? (
        <>
          <p className="muted">{accountsOnly ? 'This connection signs in with one of your own accounts.'
            : 'Choose the login for this session.'}</p>
          {choices.total > 6 && <input placeholder="Filter…" value={filter} onChange={(e) => setFilter(e.target.value)} autoFocus />}
          {choices.loading && <p className="muted">Loading…</p>}
          {!choices.loading && !choices.total && (
            <p className="banner warn">{accountsOnly ? 'You have not stored your accounts yet. Add them under Profile → My accounts.'
              : 'You have no accounts or credentials you are allowed to use.'}</p>
          )}
          <LoginChoiceList choices={choices} selected={selected} onSelect={setPicked}
                           onPick={(k) => finish(loginOverride(k))} />
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
  const [filter, setFilter] = useState('');
  const choices = useLoginChoices(req.connectionId, null, false, filter);
  const [mode, setMode] = useState<'choose' | 'own'>('choose');
  const [v, setV] = useState<InteractiveCredentials>({ username: '', password: '', domain: '' });
  const [picked, setPicked] = useState<LoginKey | null>(null);
  const valid = mode === 'own' ? Boolean(v.username || v.password) : Boolean(picked);
  const submit = () => {
    if (!valid) return;
    if (mode === 'choose') req.resolve(loginOverride(picked!));
    else req.resolve({ mode: 'own', credentials: typedCredentials(v) });
  };
  return (
    <Modal title={`Open ${req.connectionName} with different credentials`} onClose={() => req.resolve(null)} width={480}
           footer={<>
             <button onClick={() => req.resolve(null)}>Cancel</button>
             <button className="primary" disabled={!valid} onClick={submit}>Connect</button>
           </>}>
      <p className="muted">Used for this session only, instead of the connection's configured login.</p>
      <div className="radio-cards two">
        <label className={`radio-card ${mode === 'choose' ? 'active' : ''}`}>
          <input type="radio" name="open-with" checked={mode === 'choose'} onChange={() => setMode('choose')} />
          <b>Accounts &amp; credentials</b><small>Your accounts or a credential you may use.</small>
        </label>
        <label className={`radio-card ${mode === 'own' ? 'active' : ''}`}>
          <input type="radio" name="open-with" checked={mode === 'own'} onChange={() => setMode('own')} />
          <b>Enter credentials</b><small>Typed now, not stored.</small>
        </label>
      </div>
      {mode === 'own' ? (
        <OwnCredentialsForm protocol={req.protocol} value={v} onChange={setV} onSubmit={submit} />
      ) : (
        <>
          {choices.total > 6 && <input placeholder="Filter…" value={filter} onChange={(e) => setFilter(e.target.value)} autoFocus />}
          {!choices.loading && !choices.total && <p className="banner warn">You have no accounts or credentials you are allowed to use.</p>}
          <LoginChoiceList choices={choices} selected={picked} onSelect={setPicked}
                           onPick={(k) => req.resolve(loginOverride(k))} />
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
