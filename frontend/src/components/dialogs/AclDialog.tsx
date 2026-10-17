import { Trash, UserPlus } from 'lucide-react';
import { useEffect, useState } from 'react';
import { errorMessage, get, put } from '../../api/client';
import type { AclEntry, PermissionLevel, Role } from '../../api/types';
import { dialogs } from '../../state/dialogs';
import { Modal } from '../common/Modal';

interface Row {
  principal_type: 'user' | 'group' | 'role';
  principal_id: string;
  principal_name: string | null;
  permission: PermissionLevel;
}

const LEVELS: { value: PermissionLevel; label: string }[] = [
  { value: 'view', label: 'View' },
  { value: 'connect', label: 'Connect' },
  { value: 'edit', label: 'Edit' },
  { value: 'manage', label: 'Manage (incl. delete & permissions)' },
];

type ResourceType = 'folder' | 'connection' | 'credential' | 'cred_folder';

/** Several items at once (bulk): the grants are added to each one, existing grants stay. */
export interface AclTarget {
  resourceType: ResourceType;
  resourceId: string;
  name: string;
}

export function AclDialog({ resourceType, resourceId, name, bulk, onClose }: {
  resourceType: ResourceType;
  resourceId: string;
  name: string;
  bulk?: AclTarget[];
  onClose: () => void;
}) {
  const many = Boolean(bulk && bulk.length > 1);
  const [rows, setRows] = useState<Row[]>([]);
  const [roles, setRoles] = useState<Role[]>([]);
  const [groups, setGroups] = useState<{ id: string; name: string }[]>([]);
  const [userQuery, setUserQuery] = useState('');
  const [userHits, setUserHits] = useState<{ id: string; username: string; display_name: string | null }[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!many) {
      get<AclEntry[]>(`/api/v1/acl/${resourceType}/${resourceId}`).then((list) => setRows(list.map((e) => ({ ...e })))).catch((e) => setError(errorMessage(e)));
    }
    get<Role[]>('/api/v1/roles').then(setRoles).catch(() => undefined);
    get<{ id: string; name: string }[]>('/api/v1/groups').then(setGroups).catch(() => undefined);
  }, [resourceType, resourceId, many]);

  useEffect(() => {
    if (userQuery.trim().length < 2) {
      setUserHits([]);
      return undefined;
    }
    const t = window.setTimeout(() => {
      get<typeof userHits>('/api/v1/users/lookup', { q: userQuery.trim() }).then(setUserHits).catch(() => setUserHits([]));
    }, 250);
    return () => window.clearTimeout(t);
  }, [userQuery]);

  const add = (r: Omit<Row, 'permission'>) => {
    if (rows.some((x) => x.principal_type === r.principal_type && x.principal_id === r.principal_id)) return;
    setRows([...rows, { ...r, permission: 'connect' }]);
    setUserQuery('');
  };

  const save = async () => {
    if (many) {
      await saveBulk();
      return;
    }
    try {
      await put(`/api/v1/acl/${resourceType}/${resourceId}`, rows.map(({ principal_type, principal_id, permission }) => ({ principal_type, principal_id, permission })));
      dialogs.toast('Permissions saved', 'success');
      onClose();
    } catch (err) {
      setError(errorMessage(err));
    }
  };

  /** Add (or change) the rows' grants on every target; grants of other principals stay untouched. */
  const saveBulk = async () => {
    const failures: string[] = [];
    for (const t of bulk ?? []) {
      try {
        const current = await get<AclEntry[]>(`/api/v1/acl/${t.resourceType}/${t.resourceId}`);
        const merged = new Map(current.map((e) => [`${e.principal_type}:${e.principal_id}`,
          { principal_type: e.principal_type, principal_id: e.principal_id, permission: e.permission }]));
        for (const r of rows) {
          merged.set(`${r.principal_type}:${r.principal_id}`,
            { principal_type: r.principal_type, principal_id: r.principal_id, permission: r.permission });
        }
        await put(`/api/v1/acl/${t.resourceType}/${t.resourceId}`, [...merged.values()]);
      } catch (err) {
        failures.push(`${t.name}: ${errorMessage(err)}`);
      }
    }
    const done = (bulk?.length ?? 0) - failures.length;
    if (done) dialogs.toast(`Permissions added to ${done} item${done === 1 ? '' : 's'}`, 'success');
    if (failures.length) setError(`Not changed: ${failures.join('; ')}`);
    else onClose();
  };

  return (
    <Modal title={many ? `Permissions — ${bulk!.length} items` : `Permissions — ${name}`} onClose={onClose} width={620}
           footer={<>
             {error && <span className="form-error">{error}</span>}
             <button onClick={onClose}>Cancel</button>
             <button className="primary" onClick={save}>Save</button>
           </>}>
      {many ? (
        <p className="muted">The grants below are <b>added</b> to each of the {bulk!.length} selected items (and apply to everything
          below them). Existing grants stay; a principal that already has a grant gets the permission chosen here.</p>
      ) : (
        <p className="muted">Grants apply to this {resourceType} and everything below it. Owners and administrators always have full access.
          A role’s cap (e.g. read-only) limits what its members can do.</p>
      )}
      <table className="grid">
        <thead><tr><th>Principal</th><th>Type</th><th>Permission</th><th /></tr></thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={`${r.principal_type}-${r.principal_id}`}>
              <td>{r.principal_name ?? r.principal_id}</td>
              <td className="muted">{r.principal_type}</td>
              <td>
                <select value={r.permission} onChange={(e) => setRows(rows.map((x, j) => (j === i ? { ...x, permission: e.target.value as PermissionLevel } : x)))}>
                  {LEVELS.map((l) => <option key={l.value} value={l.value}>{l.label}</option>)}
                </select>
              </td>
              <td><button className="icon-btn danger" onClick={() => setRows(rows.filter((_, j) => j !== i))} aria-label="Remove"><Trash size={14} /></button></td>
            </tr>
          ))}
          {!rows.length && <tr><td colSpan={4} className="muted">{many ? 'Add a role, group or user below.' : 'No explicit grants.'}</td></tr>}
        </tbody>
      </table>
      <div className="acl-add">
        <label className="field"><span>Add role</span>
          <select value="" onChange={(e) => { const r = roles.find((x) => x.id === e.target.value); if (r) add({ principal_type: 'role', principal_id: r.id, principal_name: r.name }); }}>
            <option value="">—</option>
            {roles.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
          </select>
        </label>
        <label className="field"><span>Add group</span>
          <select value="" onChange={(e) => { const g = groups.find((x) => x.id === e.target.value); if (g) add({ principal_type: 'group', principal_id: g.id, principal_name: g.name }); }}>
            <option value="">—</option>
            {groups.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
          </select>
        </label>
        <label className="field"><span><UserPlus size={12} /> Add user</span>
          <input value={userQuery} onChange={(e) => setUserQuery(e.target.value)} placeholder="search…" />
          {userHits.length > 0 && (
            <ul className="suggestions">
              {userHits.map((u) => (
                <li key={u.id} onClick={() => add({ principal_type: 'user', principal_id: u.id, principal_name: u.username })}>
                  {u.username}{u.display_name ? ` — ${u.display_name}` : ''}
                </li>
              ))}
            </ul>
          )}
        </label>
      </div>
    </Modal>
  );
}
