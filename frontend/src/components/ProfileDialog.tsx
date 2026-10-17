import { useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { put } from '../api/client';
import { keys, usePersonalAccounts } from '../api/hooks';
import type { Me, PersonalAccount, PersonalAccountKind } from '../api/types';
import { KEYBOARD_LAYOUTS } from '../sessions/keyboardLayouts';
import { loadPreferences, savePreference } from '../sessions/registry';
import { dialogs } from '../state/dialogs';
import { Modal } from './common/Modal';

interface AccountForm {
  username: string;
  domain: string;
  /** new password; empty = keep the stored one */
  password: string;
  clearPassword: boolean;
  dirty: boolean;
}

const ACCOUNTS: { kind: PersonalAccountKind; title: string; help: string }[] = [
  { kind: 'user', title: 'User account', help: 'Your normal login, e.g. for workstations and servers you use day to day.' },
  { kind: 'privileged', title: 'Privileged account', help: 'Your administrative login (admin / tier account).' },
];

function toForm(a: PersonalAccount | undefined): AccountForm {
  return { username: a?.username ?? '', domain: a?.domain ?? '', password: '', clearPassword: false, dirty: false };
}

/** Username / domain / password of one personal account; the stored password is never shown. */
function AccountFields({ title, help, stored, value, onChange }: {
  title: string; help: string; stored: PersonalAccount | undefined; value: AccountForm; onChange: (v: AccountForm) => void;
}) {
  const set = (patch: Partial<AccountForm>) => onChange({ ...value, ...patch, dirty: true });
  const hasPassword = Boolean(stored?.has_password) && !value.clearPassword;
  return (
    <fieldset className="account-fields">
      <legend>{title}</legend>
      <small className="muted">{help}</small>
      <div className="form-grid">
        <label className="field"><span>Username</span>
          <input value={value.username} onChange={(e) => set({ username: e.target.value })} autoComplete="off" spellCheck={false} />
        </label>
        <label className="field"><span>Domain</span>
          <input value={value.domain} onChange={(e) => set({ domain: e.target.value })} autoComplete="off" spellCheck={false}
                 placeholder="optional" />
        </label>
        <label className="field span2"><span>Password</span>
          <div className="input-row">
            <input type="password" value={value.password} autoComplete="new-password"
                   placeholder={hasPassword ? 'stored – leave empty to keep' : 'not stored'}
                   onChange={(e) => set({ password: e.target.value, clearPassword: false })} />
            {hasPassword && <button type="button" onClick={() => set({ password: '', clearPassword: true })}>Remove</button>}
          </div>
        </label>
      </div>
    </fieldset>
  );
}

/** Per-user settings, stored with the other preferences on the server. */
export function ProfileDialog({ me, onClose }: { me: Me | undefined; onClose: () => void }) {
  const [layout, setLayout] = useState<string | null>(null);
  const [commandAsCtrl, setCommandAsCtrl] = useState(true);
  const { data: accounts, isLoading: accountsLoading } = usePersonalAccounts();
  const [forms, setForms] = useState<Record<PersonalAccountKind, AccountForm> | null>(null);
  const [saving, setSaving] = useState(false);
  const qc = useQueryClient();

  useEffect(() => {
    void loadPreferences().then((p) => {
      setLayout(typeof p.keyboard_layout === 'string' ? p.keyboard_layout : '');
      setCommandAsCtrl(p.mac_command_as_ctrl !== false);
    });
  }, []);

  useEffect(() => {
    if (accounts && !forms) {
      const byKind = new Map(accounts.map((a) => [a.kind, a]));
      setForms({ user: toForm(byKind.get('user')), privileged: toForm(byKind.get('privileged')) });
    }
  }, [accounts, forms]);

  const stored = (kind: PersonalAccountKind) => accounts?.find((a) => a.kind === kind);

  const save = async () => {
    setSaving(true);
    try {
      await savePreference('keyboard_layout', layout || null);
      await savePreference('mac_command_as_ctrl', commandAsCtrl);
      for (const { kind } of ACCOUNTS) {
        const f = forms?.[kind];
        if (!f?.dirty) continue;
        await put(`/api/v1/me/accounts/${kind}`, {
          username: f.username.trim() || null,
          domain: f.domain.trim() || null,
          ...(f.password ? { password: f.password } : {}),
          clear_password: f.clearPassword,
        });
      }
      void qc.invalidateQueries({ queryKey: keys.personalAccounts });
      dialogs.toast('Profile saved. The keyboard layout applies to RDP sessions opened from now on.', 'success');
      onClose();
    } catch (err) {
      dialogs.toast(`Saving the profile failed: ${err instanceof Error ? err.message : String(err)}`, 'error');
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal title="Profile" onClose={onClose} width={560}
           footer={<><button onClick={onClose}>Cancel</button>
             <button className="primary" disabled={layout === null || saving || (accountsLoading && !forms)}
                     onClick={() => void save()}>Save</button></>}>
      <dl className="profile-facts">
        <dt>User</dt><dd>{me?.display_name ? `${me.display_name} (${me.username})` : me?.username}</dd>
        {me?.email && <><dt>E-mail</dt><dd>{me.email}</dd></>}
        <dt>Roles</dt><dd>{me?.roles.join(', ') || '–'}</dd>
      </dl>
      <label className="field"><span>Keyboard layout for remote desktops (RDP)</span>
        <select value={layout ?? ''} disabled={layout === null} onChange={(e) => setLayout(e.target.value)}>
          <option value="">English (US) – default</option>
          {KEYBOARD_LAYOUTS.map((l) => <option key={l.value} value={l.value}>{l.label}</option>)}
        </select>
        <small className="muted">Choose the layout set on the remote computers, normally the one you type with. A layout set
          on a connection overrides it.</small>
      </label>
      <label className="check">
        <input type="checkbox" checked={commandAsCtrl} onChange={(e) => setCommandAsCtrl(e.target.checked)} />
        <span>Mac: Command key acts as Ctrl in remote desktops</span>
        <small className="muted">⌘C / ⌘V / ⌘A copy, paste and select all in Windows. Turn off to send ⌘ as the Windows key.</small>
      </label>

      <h3 className="profile-heading">My accounts</h3>
      <p className="muted">Your own logins. Choose them under “My accounts” when a connection lets you pick the credentials,
        or with “Open with different credentials”. Only you can use them; passwords are stored encrypted and never shown again.</p>
      {!forms ? <p className="muted">Loading…</p> : ACCOUNTS.map((a) => (
        <AccountFields key={a.kind} title={a.title} help={a.help} stored={stored(a.kind)} value={forms[a.kind]}
                       onChange={(v) => setForms({ ...forms, [a.kind]: v })} />
      ))}
    </Modal>
  );
}
