/** Special keys offered on touch devices (phone keyboards lack them). */

export interface Modifiers {
  ctrl: boolean;
  alt: boolean;
  shift: boolean;
  meta: boolean;
}

export const NO_MODIFIERS: Modifiers = { ctrl: false, alt: false, shift: false, meta: false };

export type SpecialKey =
  | 'Escape' | 'Tab' | 'Enter' | 'Backspace' | 'Delete' | 'Insert'
  | 'ArrowUp' | 'ArrowDown' | 'ArrowLeft' | 'ArrowRight'
  | 'Home' | 'End' | 'PageUp' | 'PageDown'
  | 'F1' | 'F2' | 'F3' | 'F4' | 'F5' | 'F6' | 'F7' | 'F8' | 'F9' | 'F10' | 'F11' | 'F12';

/** Implemented by sessions that accept keyboard input (terminal, remote desktop). */
export interface SpecialKeyTarget {
  /** Modifiers latched on the key bar; they apply to the next key typed (soft keyboard or bar). */
  setLatchedModifiers(mods: Modifiers, onConsumed: () => void): void;
  /** Send a special key, combined with the latched modifiers (which are then released). */
  sendSpecialKey(key: SpecialKey): void;
  /** Bring up the device's on-screen keyboard. */
  showKeyboard(): void;
}

export function anyModifier(m: Modifiers): boolean {
  return m.ctrl || m.alt || m.shift || m.meta;
}

// ---------------------------------------------------------------- X11 keysyms (Guacamole)

export const MODIFIER_KEYSYMS: Record<keyof Modifiers, number> = {
  ctrl: 0xffe3, // Control_L
  alt: 0xffe9, // Alt_L
  shift: 0xffe1, // Shift_L
  meta: 0xffeb, // Super_L (Windows key)
};

const FUNCTION_KEYSYM_BASE = 0xffbe; // F1

export function specialKeysym(key: SpecialKey): number {
  if (/^F\d+$/.test(key)) return FUNCTION_KEYSYM_BASE + Number(key.slice(1)) - 1;
  return {
    Escape: 0xff1b, Tab: 0xff09, Enter: 0xff0d, Backspace: 0xff08, Delete: 0xffff, Insert: 0xff63,
    ArrowUp: 0xff52, ArrowDown: 0xff54, ArrowLeft: 0xff51, ArrowRight: 0xff53,
    Home: 0xff50, End: 0xff57, PageUp: 0xff55, PageDown: 0xff56,
  }[key as Exclude<SpecialKey, `F${number}`>];
}

// ---------------------------------------------------------------- terminal byte sequences (xterm)

/** xterm modifier parameter: 1 + shift + 2*alt + 4*ctrl. */
function modParam(m: Modifiers): number {
  return 1 + (m.shift ? 1 : 0) + (m.alt ? 2 : 0) + (m.ctrl ? 4 : 0);
}

export function terminalSequence(key: SpecialKey, m: Modifiers): string {
  const p = modParam(m);
  const csi = (final: string) => (p > 1 ? `\x1b[1;${p}${final}` : `\x1b[${final}`);
  const tilde = (n: number) => (p > 1 ? `\x1b[${n};${p}~` : `\x1b[${n}~`);
  const alt = (s: string) => (m.alt ? `\x1b${s}` : s);
  switch (key) {
    case 'Escape': return '\x1b';
    case 'Tab': return m.shift ? '\x1b[Z' : alt('\t');
    case 'Enter': return alt('\r');
    case 'Backspace': return alt(m.ctrl ? '\x08' : '\x7f');
    case 'ArrowUp': return csi('A');
    case 'ArrowDown': return csi('B');
    case 'ArrowRight': return csi('C');
    case 'ArrowLeft': return csi('D');
    case 'Home': return csi('H');
    case 'End': return csi('F');
    case 'Insert': return tilde(2);
    case 'Delete': return tilde(3);
    case 'PageUp': return tilde(5);
    case 'PageDown': return tilde(6);
    case 'F1': return p > 1 ? `\x1b[1;${p}P` : '\x1bOP';
    case 'F2': return p > 1 ? `\x1b[1;${p}Q` : '\x1bOQ';
    case 'F3': return p > 1 ? `\x1b[1;${p}R` : '\x1bOR';
    case 'F4': return p > 1 ? `\x1b[1;${p}S` : '\x1bOS';
    default: {
      const codes: Record<string, number> = { F5: 15, F6: 17, F7: 18, F8: 19, F9: 20, F10: 21, F11: 23, F12: 24 };
      return tilde(codes[key]);
    }
  }
}

/** Apply latched modifiers to text typed on the soft keyboard (one character at a time). */
export function applyModifiersToText(text: string, m: Modifiers): string {
  let out = text;
  if (m.shift) out = out.toUpperCase();
  if (m.ctrl && out.length === 1) {
    const c = out.toUpperCase().charCodeAt(0);
    if (c >= 0x40 && c <= 0x5f) out = String.fromCharCode(c & 0x1f); // Ctrl+A..Z, @ [ \ ] ^ _
    else if (out === ' ') out = '\x00';
    else if (out === '?') out = '\x7f';
  }
  if (m.alt) out = `\x1b${out}`;
  return out;
}
