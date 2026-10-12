import { useState } from 'react';
import { errorMessage, patch, post } from '../../api/client';
import { useInvalidate } from '../../api/hooks';
import type { Folder } from '../../api/types';
import { dialogs } from '../../state/dialogs';
import { FOLDER_COLORS, FOLDER_ICONS } from '../common/icons';
import { Modal } from '../common/Modal';

export function FolderDialog({ folder, parentId, onClose }: { folder?: Folder; parentId?: string | null; onClose: () => void }) {
  const invalidate = useInvalidate();
  const [name, setName] = useState(folder?.name ?? '');
  const [icon, setIcon] = useState(folder?.icon ?? 'folder');
  const [color, setColor] = useState(folder?.color ?? '');
  const [description, setDescription] = useState(folder?.description ?? '');
  const [searchDomain, setSearchDomain] = useState(folder?.search_domain ?? '');
  const [error, setError] = useState<string | null>(null);

  const save = async () => {
    const body = {
      name: name.trim(),
      icon: icon === 'folder' ? null : icon,
      color: color || null,
      description: description.trim() || null,
      search_domain: searchDomain.trim() || null,
    };
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
    <Modal title={folder ? `Edit folder ${folder.name}` : 'New folder'} onClose={onClose} width={480}
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
    </Modal>
  );
}
