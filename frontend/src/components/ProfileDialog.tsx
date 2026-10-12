import { useEffect, useState } from 'react';
import type { Me } from '../api/types';
import { KEYBOARD_LAYOUTS } from '../sessions/keyboardLayouts';
import { loadPreferences, savePreference } from '../sessions/registry';
import { dialogs } from '../state/dialogs';
import { Modal } from './common/Modal';

/** Per-user settings, stored with the other preferences on the server. */
export function ProfileDialog({ me, onClose }: { me: Me | undefined; onClose: () => void }) {
  const [layout, setLayout] = useState<string | null>(null);
  const [commandAsCtrl, setCommandAsCtrl] = useState(true);

  useEffect(() => {
    void loadPreferences().then((p) => {
      setLayout(typeof p.keyboard_layout === 'string' ? p.keyboard_layout : '');
      setCommandAsCtrl(p.mac_command_as_ctrl !== false);
    });
  }, []);

  const save = async () => {
    await savePreference('keyboard_layout', layout || null);
    await savePreference('mac_command_as_ctrl', commandAsCtrl);
    dialogs.toast('Profile saved. The keyboard layout applies to RDP sessions opened from now on.', 'success');
    onClose();
  };

  return (
    <Modal title="Profile" onClose={onClose} width={480}
           footer={<><button onClick={onClose}>Cancel</button><button className="primary" disabled={layout === null} onClick={() => void save()}>Save</button></>}>
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
    </Modal>
  );
}
