import { Pencil, Play, ShieldAlert, X } from 'lucide-react';
import { useEffect } from 'react';
import { useEffective } from '../api/hooks';
import type { Connection, ResolvedValue } from '../api/types';
import { openConnection } from '../sessions/registry';
import { PROTOCOL_LABELS, ProtocolIcon } from './common/icons';

function Source({ r }: { r: ResolvedValue | null | undefined }) {
  if (!r) return null;
  const s = r.source;
  const label = s.kind === 'default' ? 'default' : s.kind === 'environment' ? 'server default'
    : s.kind === 'interactive' ? 'prompt' : `${s.kind}: ${s.name ?? ''}`;
  return <span className={`src ${r.inherited ? 'inherited' : 'explicit'}`}>{r.inherited ? 'inherited · ' : ''}{label}</span>;
}

export function DetailsPanel({ connection, onClose, onEdit }: { connection: Connection; onClose: () => void; onEdit: () => void }) {
  const { data: eff, refetch } = useEffective(connection.id);
  useEffect(() => {
    void refetch();
  }, [connection.version, refetch]);
  const canConnect = ['connect', 'edit', 'manage'].includes(connection.permission ?? '') && connection.protocol !== 'host';
  const auth = eff?.auth ?? {};
  return (
    <aside className="details-panel">
      <div className="details-head">
        <ProtocolIcon protocol={connection.protocol} size={18} />
        <h3 title={connection.name}>{connection.name}</h3>
        <button className="icon-btn" onClick={onClose} aria-label="Close details"><X size={14} /></button>
      </div>
      <div className="row">
        <button className="primary" disabled={!canConnect} onClick={() => void openConnection(connection)}><Play size={14} /> Connect</button>
        <button disabled={!['edit', 'manage'].includes(connection.permission ?? '')} onClick={onEdit}><Pencil size={14} /> Edit</button>
      </div>
      {connection.warnings.map((w) => <div key={w.code} className="banner warn"><ShieldAlert size={14} /> {w.message}</div>)}
      <dl className="kv">
        <dt>Protocol</dt><dd>{PROTOCOL_LABELS[connection.protocol]}</dd>
        <dt>Host</dt><dd>{eff?.hostname?.value ?? <span className="muted">not set</span>} <Source r={eff?.hostname} /></dd>
        {eff && eff.hostname_candidates.length > 1 && <><dt>Resolves as</dt><dd className="small">{eff.hostname_candidates.join(' → ')}</dd></>}
        <dt>Port</dt><dd>{eff?.port?.value ?? '—'} <Source r={eff?.port} /></dd>
        {eff?.search_domains && <><dt>Search domain</dt><dd>{eff.search_domains.value?.join(', ')} <Source r={eff.search_domains} /></dd></>}
        <dt>Authentication</dt><dd>{connection.auth_mode}{eff?.prompt_required ? ' (asks at connect)' : ''}
          {eff?.credential_choice_required ? ' (credential chosen at connect)' : ''}</dd>
        <dt>Username</dt><dd>{auth.username?.value ?? <span className="muted">—</span>} <Source r={auth.username} /></dd>
        <dt>Password</dt><dd>{auth.password?.set ? '••••••' : <span className="muted">—</span>} <Source r={auth.password} /></dd>
        {auth.domain && <><dt>Domain</dt><dd>{auth.domain.value} <Source r={auth.domain} /></dd></>}
        {auth.private_key && <><dt>SSH key</dt><dd>set <Source r={auth.private_key} /></dd></>}
        {connection.tags.length > 0 && <><dt>Tags</dt><dd>{connection.tags.map((t) => <span key={t} className="badge">{t}</span>)}</dd></>}
        <dt>Your access</dt><dd>{connection.permission}</dd>
      </dl>
      {eff && eff.chain.length > 1 && (
        <div className="small muted">Inheritance chain: {eff.chain.map((c) => c.name).join(' ← ')}</div>
      )}
      {connection.description && <p className="description">{connection.description}</p>}
    </aside>
  );
}
