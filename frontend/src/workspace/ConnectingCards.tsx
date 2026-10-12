import { LoaderCircle, X } from 'lucide-react';
import { useEffect, useState } from 'react';
import { ProtocolIcon } from '../components/common/icons';
import { cancelConnect, useConnecting } from '../state/connecting';

/** Seconds since `since`, updated every second. */
function useElapsed(since: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, []);
  return Math.max(0, Math.floor((now - since) / 1000));
}

function Card({ id, name, protocol, step, startedAt, cancelled }: {
  id: string; name: string; protocol: Parameters<typeof ProtocolIcon>[0]['protocol']; step: string; startedAt: number; cancelled: boolean;
}) {
  const elapsed = useElapsed(startedAt);
  return (
    <div className="connecting-card" role="status">
      <LoaderCircle className="spin" size={22} />
      <div className="connecting-text">
        <div className="connecting-name"><ProtocolIcon protocol={protocol} /> {name}</div>
        <div className="muted small">{cancelled ? 'Cancelling…' : step}{elapsed >= 3 ? ` · ${elapsed} s` : ''}</div>
      </div>
      {!cancelled && (
        <button className="icon-btn" title="Cancel" onClick={() => cancelConnect(id)}><X size={14} /></button>
      )}
    </div>
  );
}

/**
 * Connections being opened, shown from the click until the session window appears (the server
 * resolves credentials, checks host keys and starts the session in the meantime).
 */
export function ConnectingCards() {
  const all = useConnecting((s) => s.pending);
  const pending = all.filter((p) => !p.waiting); // a dialog is asking the user meanwhile
  if (!pending.length) return null;
  return (
    <div className="connecting-layer">
      {pending.map((p) => (
        <Card key={p.key} id={p.key} name={p.name} protocol={p.protocol} step={p.step} startedAt={p.startedAt} cancelled={p.cancelled} />
      ))}
    </div>
  );
}
