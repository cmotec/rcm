import { FileUp, TriangleAlert } from 'lucide-react';
import { useState } from 'react';
import { api, errorMessage, post } from '../../api/client';
import { useInvalidate, useTree } from '../../api/hooks';

interface PreviewItem {
  source_id: string;
  kind: 'folder' | 'connection' | 'credential' | 'unsupported';
  name: string;
  entry_type: string;
  protocol: string | null;
  host: string | null;
  port: number | null;
  folder_path: string[];
  parent_source_id: string | null;
  credential_status: string;
  unsupported: { setting: string; value: string }[];
  warnings: string[];
  duplicate_of: string | null;
  duplicate_reason: string | null;
}

interface ImportJob {
  id: string;
  status: string;
  preview: { format: string; items?: PreviewItem[]; summary: Record<string, unknown> };
  result: null | {
    created: Record<string, number>;
    skipped: { name: string; reason: string }[];
    errors: { name: string; error: string }[];
    manual_migration: { name: string; reason: string; id: string }[];
    unsupported_settings: { name: string; settings: { setting: string; value: string }[] }[];
    warnings: { name: string; message: string }[];
  };
  manual_migration_help: string;
}

export function ImportWizard() {
  const { data: tree } = useTree();
  const invalidate = useInvalidate();
  const [job, setJob] = useState<ImportJob | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [target, setTarget] = useState('');
  const [onDuplicate, setOnDuplicate] = useState<'skip' | 'create'>('skip');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const upload = async (file: File) => {
    setBusy(true);
    setError(null);
    try {
      const fd = new FormData();
      fd.append('file', file);
      const j = await api<ImportJob>('/api/v1/imports/rdm', { method: 'POST', body: fd });
      setJob(j);
      setSelected(new Set((j.preview.items ?? []).filter((i) => i.kind !== 'unsupported' && !i.duplicate_of).map((i) => i.source_id)));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const apply = async () => {
    if (!job) return;
    setBusy(true);
    try {
      const j = await post<ImportJob>(`/api/v1/imports/${job.id}/apply`, {
        selected: [...selected], target_folder_id: target || null, on_duplicate: onDuplicate,
      });
      setJob({ ...job, ...j });
      invalidate.tree();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const items = job?.preview.items ?? [];
  const summary = job?.preview.summary as { by_kind?: Record<string, number>; unsupported_types?: Record<string, number>;
    encrypted_credentials?: number; duplicates?: number; errors?: string[] } | undefined;

  return (
    <div>
      <p className="muted">Import connections, folders and credential entries from a Devolutions Remote Desktop Manager export
        (<code>.rdm</code> XML or CSV). You get a preview first and choose what to import. Encrypted passwords are never decrypted —
        affected entries are listed for manual migration.</p>
      {error && <div className="banner error">{error}</div>}
      {!job && (
        <label className="dropzone">
          <FileUp size={22} />
          <span>{busy ? 'Analysing…' : 'Choose an RDM export file'}</span>
          <input type="file" accept=".rdm,.xml,.csv" hidden onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])} />
        </label>
      )}
      {job && !job.result && (
        <>
          <div className="summary-chips">
            {Object.entries(summary?.by_kind ?? {}).map(([k, v]) => <span key={k} className="badge">{v} {k}</span>)}
            {summary?.duplicates ? <span className="badge warn">{summary.duplicates} possible duplicates</span> : null}
            {summary?.encrypted_credentials ? <span className="badge warn">{summary.encrypted_credentials} encrypted passwords (not migrated)</span> : null}
            {Object.entries(summary?.unsupported_types ?? {}).map(([k, v]) => <span key={k} className="badge danger">{v} × unsupported “{k}”</span>)}
          </div>
          {summary?.errors?.map((e) => <div key={e} className="banner error">{e}</div>)}
          <div className="row">
            <label className="field"><span>Import into folder</span>
              <select value={target} onChange={(e) => setTarget(e.target.value)}>
                <option value="">Root</option>
                {(tree?.folders ?? []).map((f) => <option key={f.id} value={f.id}>{f.name}</option>)}
              </select>
            </label>
            <label className="field"><span>Duplicates</span>
              <select value={onDuplicate} onChange={(e) => setOnDuplicate(e.target.value as 'skip' | 'create')}>
                <option value="skip">Skip</option><option value="create">Import anyway</option>
              </select>
            </label>
          </div>
          <div className="table-scroll">
            <table className="grid small">
              <thead><tr>
                <th><input type="checkbox" checked={selected.size === items.filter((i) => i.kind !== 'unsupported').length}
                           onChange={(e) => setSelected(new Set(e.target.checked ? items.filter((i) => i.kind !== 'unsupported').map((i) => i.source_id) : []))} /></th>
                <th>Name</th><th>Type</th><th>Target</th><th>Folder</th><th>Credentials</th><th>Notes</th>
              </tr></thead>
              <tbody>
                {items.map((i) => (
                  <tr key={i.source_id} className={i.kind === 'unsupported' ? 'disabled' : ''}>
                    <td><input type="checkbox" disabled={i.kind === 'unsupported'} checked={selected.has(i.source_id)}
                               onChange={(e) => { const s = new Set(selected); if (e.target.checked) s.add(i.source_id); else s.delete(i.source_id); setSelected(s); }} /></td>
                    <td>{i.parent_source_id ? '↳ ' : ''}{i.name}</td>
                    <td>{i.kind === 'connection' ? i.protocol : i.kind}<div className="muted">{i.entry_type}</div></td>
                    <td className="mono">{i.host ? `${i.host}${i.port ? `:${i.port}` : ''}` : ''}</td>
                    <td>{i.folder_path.join(' / ')}</td>
                    <td>{i.credential_status !== 'none' && <span className={`badge ${i.credential_status === 'encrypted_not_migrated' ? 'warn' : ''}`}>{i.credential_status.replace(/_/g, ' ')}</span>}</td>
                    <td>
                      {i.duplicate_reason && <div className="badge warn">duplicate: {i.duplicate_reason}</div>}
                      {i.warnings.map((w) => <div key={w} className="small"><TriangleAlert size={11} /> {w}</div>)}
                      {i.unsupported.length > 0 && (
                        <details><summary>{i.unsupported.length} unsupported setting(s)</summary>
                          <ul className="small mono">{i.unsupported.map((u) => <li key={u.setting}>{u.setting} = {u.value}</li>)}</ul>
                        </details>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="row">
            <button onClick={() => setJob(null)}>Start over</button>
            <button className="primary" disabled={busy || !selected.size} onClick={apply}>Import {selected.size} item(s)</button>
          </div>
        </>
      )}
      {job?.result && (
        <div className="import-report">
          <h3>Import summary</h3>
          <div className="summary-chips">
            {Object.entries(job.result.created).map(([k, v]) => <span key={k} className="badge ok">{v} {k} created</span>)}
            <span className="badge">{job.result.skipped.length} skipped</span>
            {job.result.errors.length > 0 && <span className="badge danger">{job.result.errors.length} errors</span>}
          </div>
          {job.result.manual_migration.length > 0 && (
            <section>
              <h4>Manual credential migration required</h4>
              <p className="muted">{job.manual_migration_help}</p>
              <ul>{job.result.manual_migration.map((m) => <li key={m.id}><b>{m.name}</b> — {m.reason}</li>)}</ul>
            </section>
          )}
          {job.result.errors.length > 0 && <section><h4>Errors</h4><ul>{job.result.errors.map((e) => <li key={e.name}>{e.name}: {e.error}</li>)}</ul></section>}
          {job.result.skipped.length > 0 && <section><h4>Skipped</h4><ul>{job.result.skipped.map((s, i) => <li key={i}>{s.name}: {s.reason}</li>)}</ul></section>}
          {job.result.warnings.length > 0 && <section><h4>Warnings</h4><ul>{job.result.warnings.map((w, i) => <li key={i}>{w.name}: {w.message}</li>)}</ul></section>}
          {job.result.unsupported_settings.length > 0 && (
            <section><h4>Settings that could not be mapped</h4>
              {job.result.unsupported_settings.map((u) => (
                <details key={u.name}><summary>{u.name} ({u.settings.length})</summary>
                  <ul className="small mono">{u.settings.map((s) => <li key={s.setting}>{s.setting} = {s.value}</li>)}</ul>
                </details>
              ))}
            </section>
          )}
          <button onClick={() => setJob(null)}>Import another file</button>
        </div>
      )}
    </div>
  );
}
