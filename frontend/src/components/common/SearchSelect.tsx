import { ChevronDown, X } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';

export interface SearchOption {
  value: string;
  label: string;
  /** shown greyed next to the label, also searched (e.g. the username or host name) */
  hint?: string;
}

/**
 * A select with a search field: type to filter, click or Enter to choose. ``value`` "" is the empty
 * choice, labelled ``emptyLabel`` (e.g. "None").
 */
export function SearchSelect({ value, onChange, options, emptyLabel, placeholder = 'Search…' }: {
  value: string; onChange: (value: string) => void; options: SearchOption[]; emptyLabel: string; placeholder?: string;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [active, setActive] = useState(0);
  const box = useRef<HTMLDivElement>(null);
  const list = useRef<HTMLUListElement>(null);
  const current = options.find((o) => o.value === value);

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    const hits = q ? options.filter((o) => `${o.label} ${o.hint ?? ''}`.toLowerCase().includes(q)) : options;
    return [{ value: '', label: emptyLabel }, ...hits];
  }, [options, query, emptyLabel]);

  useEffect(() => {
    if (!open) return;
    const close = (e: PointerEvent) => {
      if (!box.current?.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener('pointerdown', close);
    return () => window.removeEventListener('pointerdown', close);
  }, [open]);

  useEffect(() => {
    list.current?.children[active]?.scrollIntoView({ block: 'nearest' });
  }, [active]);

  const choose = (v: string) => {
    onChange(v);
    setOpen(false);
    setQuery('');
  };

  return (
    <div className="search-select" ref={box}>
      <div className="search-select-input">
        <input
          value={open ? query : current ? `${current.label}${current.hint ? ` (${current.hint})` : ''}` : emptyLabel}
          placeholder={placeholder}
          onFocus={() => { setOpen(true); setQuery(''); setActive(0); }}
          onChange={(e) => { setQuery(e.target.value); setActive(0); setOpen(true); }}
          onKeyDown={(e) => {
            if (e.key === 'ArrowDown') { e.preventDefault(); setOpen(true); setActive((i) => Math.min(i + 1, shown.length - 1)); }
            else if (e.key === 'ArrowUp') { e.preventDefault(); setActive((i) => Math.max(i - 1, 0)); }
            else if (e.key === 'Enter' && open) { e.preventDefault(); if (shown[active]) choose(shown[active].value); }
            else if (e.key === 'Escape' && open) { e.stopPropagation(); setOpen(false); setQuery(''); }
            else if (e.key === 'Tab') setOpen(false);
          }}
          spellCheck={false}
          autoComplete="off"
          role="combobox"
          aria-expanded={open}
        />
        {value && !open
          ? <button type="button" className="icon-btn" title="Clear" onClick={() => choose('')}><X size={13} /></button>
          : <ChevronDown size={14} className="muted" />}
      </div>
      {open && (
        <ul className="search-select-list" role="listbox" ref={list}>
          {shown.map((o, i) => (
            <li key={o.value || '__empty'} role="option" aria-selected={o.value === value}
                className={`${i === active ? 'active' : ''} ${o.value === value ? 'chosen' : ''} ${o.value ? '' : 'muted'}`}
                onPointerDown={(e) => { e.preventDefault(); choose(o.value); }}
                onPointerEnter={() => setActive(i)}>
              <span>{o.label}</span>{o.hint && <span className="muted"> {o.hint}</span>}
            </li>
          ))}
          {shown.length === 1 && query && <li className="muted no-hit">No matches</li>}
        </ul>
      )}
    </div>
  );
}
