import { create } from 'zustand';
import type { Connection } from '../api/types';
import { savePreference } from '../sessions/registry';

/** How connections are labelled in the tree (per user). */
export interface TreePrefs {
  /** Which value is shown first; the other one follows in parentheses. */
  order: 'hostname' | 'description';
  /** Show only the description (or the name when there is none). */
  hideHostname: boolean;
}

const DEFAULTS: TreePrefs = { order: 'hostname', hideHostname: false };
const STORAGE_KEY = 'tree.prefs';

function load(): TreePrefs {
  try {
    return { ...DEFAULTS, ...(JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '{}') as Partial<TreePrefs>) };
  } catch {
    return DEFAULTS;
  }
}

interface TreePrefsState extends TreePrefs {
  update: (patch: Partial<TreePrefs>) => void;
  /** Apply preferences loaded from the server (they win over the browser copy). */
  hydrate: (prefs: unknown) => void;
}

export const useTreePrefs = create<TreePrefsState>((set, get) => ({
  ...load(),
  update: (patch) => {
    set(patch);
    const { order, hideHostname } = get();
    const value: TreePrefs = { order, hideHostname };
    localStorage.setItem(STORAGE_KEY, JSON.stringify(value));
    void savePreference('tree', value);
  },
  hydrate: (prefs) => {
    if (!prefs || typeof prefs !== 'object') return;
    const p = prefs as Partial<TreePrefs>;
    const next: TreePrefs = {
      order: p.order === 'description' ? 'description' : 'hostname',
      hideHostname: Boolean(p.hideHostname),
    };
    localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
    set(next);
  },
}));

/** First line of the description, trimmed (the tree shows one line). */
function shortDescription(c: Connection): string {
  return (c.resolved?.description ?? c.description ?? '').split(/\r?\n/)[0].trim();
}

/**
 * Tree label of a connection: `hostname (description)` by default, `description (hostname)` when
 * switched, `description` when the hostname is hidden. Entries without their own hostname (host
 * containers, children that inherit it) use their name instead. The domain is never shown.
 */
export function connectionLabel(c: Connection, prefs: TreePrefs): { primary: string; secondary: string | null } {
  const host = (c.resolved?.hostname ?? c.hostname)?.trim() || null;
  const desc = shortDescription(c) || null;
  if (!host) return { primary: c.name, secondary: desc && desc !== c.name ? desc : null };
  if (prefs.hideHostname) return { primary: desc ?? c.name, secondary: null };
  if (prefs.order === 'description' && desc) return { primary: desc, secondary: host };
  return { primary: host, secondary: desc };
}
