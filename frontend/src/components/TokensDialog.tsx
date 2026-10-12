import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Copy, Trash } from 'lucide-react';
import { useState } from 'react';
import { del, errorMessage, get, post } from '../api/client';
import type { ApiToken } from '../api/types';
import { dialogs } from '../state/dialogs';
import { formatDate } from '../util/download';
import { Modal } from './common/Modal';

export function TokensDialog({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const tokens = useQuery({ queryKey: ['tokens'], queryFn: () => get<ApiToken[]>('/api/v1/me/tokens') });
  const [name, setName] = useState('');
  const [days, setDays] = useState('90');
  const [readOnly, setReadOnly] = useState(false);
  const [created, setCreated] = useState<string | null>(null);
  const create = async () => {
    try {
      const t = await post<ApiToken>('/api/v1/me/tokens', { name, expires_in_days: days ? Number(days) : null, read_only: readOnly });
      setCreated(t.token ?? null);
      setName('');
      void qc.invalidateQueries({ queryKey: ['tokens'] });
    } catch (err) {
      dialogs.toast(errorMessage(err), 'error');
    }
  };
  return (
    <Modal title="API tokens" onClose={onClose} width={680} footer={<button onClick={onClose}>Close</button>}>
      <p className="muted">Personal access tokens authenticate REST API calls (<code>Authorization: Bearer …</code>) with your permissions.
        Read-only tokens cannot change anything or reveal secrets. API documentation: <a href="/api/docs" target="_blank" rel="noreferrer">/api/docs</a></p>
      {created && (
        <div className="banner info">
          Copy the token now — it is not shown again:
          <code className="token">{created}</code>
          <button className="icon-btn" onClick={() => navigator.clipboard?.writeText(created)}><Copy size={14} /></button>
        </div>
      )}
      <div className="row">
        <input placeholder="Token name" value={name} onChange={(e) => setName(e.target.value)} />
        <input type="number" min={1} style={{ width: 90 }} value={days} onChange={(e) => setDays(e.target.value)} title="Expires in days" />
        <label className="check inline"><input type="checkbox" checked={readOnly} onChange={(e) => setReadOnly(e.target.checked)} /><span>read-only</span></label>
        <button className="primary" disabled={!name} onClick={create}>Create</button>
      </div>
      <table className="grid small">
        <thead><tr><th>Name</th><th>Prefix</th><th>Mode</th><th>Expires</th><th>Last used</th><th /></tr></thead>
        <tbody>
          {tokens.data?.map((t) => (
            <tr key={t.id} className={t.revoked_at ? 'disabled' : ''}>
              <td>{t.name}</td><td className="mono">{t.prefix}…</td><td>{t.read_only ? 'read-only' : 'full'}</td>
              <td>{formatDate(t.expires_at)}</td><td>{formatDate(t.last_used_at)}</td>
              <td>{!t.revoked_at && <button className="icon-btn danger" title="Revoke"
                                             onClick={() => del(`/api/v1/me/tokens/${t.id}`).then(() => qc.invalidateQueries({ queryKey: ['tokens'] }))}><Trash size={14} /></button>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Modal>
  );
}
