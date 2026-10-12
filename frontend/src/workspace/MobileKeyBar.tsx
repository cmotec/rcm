import { Keyboard, MousePointer2, Pointer } from 'lucide-react';
import { useEffect, useState } from 'react';
import { type Modifiers, NO_MODIFIERS, type SpecialKey, type SpecialKeyTarget } from '../sessions/specialKeys';

const MOD_KEYS: { key: keyof Modifiers; label: string; desktopOnly?: boolean }[] = [
  { key: 'ctrl', label: 'Ctrl' },
  { key: 'alt', label: 'Alt' },
  { key: 'shift', label: 'Shift' },
  { key: 'meta', label: 'Win', desktopOnly: true },
];

const KEYS: { key: SpecialKey; label: string }[] = [
  { key: 'Escape', label: 'Esc' },
  { key: 'Tab', label: 'Tab' },
  { key: 'ArrowUp', label: '↑' },
  { key: 'ArrowDown', label: '↓' },
  { key: 'ArrowLeft', label: '←' },
  { key: 'ArrowRight', label: '→' },
  { key: 'Home', label: 'Home' },
  { key: 'End', label: 'End' },
  { key: 'PageUp', label: 'PgUp' },
  { key: 'PageDown', label: 'PgDn' },
  { key: 'Delete', label: 'Del' },
  { key: 'Insert', label: 'Ins' },
];

const F_KEYS = Array.from({ length: 12 }, (_, i) => `F${i + 1}` as SpecialKey);

/** Remote-desktop extras on touch devices (implemented by GuacSession). */
export interface DesktopTouchControls {
  pointerMode: 'touchpad' | 'touchscreen';
  setPointerMode(mode: 'touchpad' | 'touchscreen'): void;
}

/**
 * Special keys for touch devices. Ctrl/Alt/Shift/Win are sticky: tap once and they apply to the next
 * key (from this bar or the phone keyboard), then release. Buttons never take focus, so the phone
 * keyboard stays open while tapping them. Remote desktops also get a pointer-mode toggle (zoom is a
 * two-finger pinch on the desktop itself).
 */
export function MobileKeyBar({ target, desktop }: { target: SpecialKeyTarget; desktop?: DesktopTouchControls }) {
  const [mods, setMods] = useState<Modifiers>(NO_MODIFIERS);
  const [fnKeys, setFnKeys] = useState(false);
  const [pointer, setPointer] = useState(desktop?.pointerMode ?? 'touchpad');

  // release latched modifiers in the session when the bar goes away
  useEffect(() => () => target.setLatchedModifiers(NO_MODIFIERS, () => undefined), [target]);

  const keepFocus = (e: React.PointerEvent | React.MouseEvent) => e.preventDefault();

  const toggleMod = (key: keyof Modifiers) => {
    const next = { ...mods, [key]: !mods[key] };
    setMods(next);
    target.setLatchedModifiers(next, () => setMods(NO_MODIFIERS));
  };

  return (
    <div className="key-bar" role="toolbar" aria-label="Special keys" onPointerDown={keepFocus} onMouseDown={keepFocus}>
      <button className="key" title="Show keyboard" onClick={() => target.showKeyboard()}><Keyboard size={15} /></button>
      {desktop && (
        <>
          <button className="key" title={pointer === 'touchpad' ? 'Pointer: touchpad (drag moves the pointer) — switch to direct'
                    : 'Pointer: direct (tap where you touch) — switch to touchpad'}
                  onClick={() => {
                    const next = pointer === 'touchpad' ? 'touchscreen' : 'touchpad';
                    desktop.setPointerMode(next);
                    setPointer(next);
                  }}>
            {pointer === 'touchpad' ? <MousePointer2 size={15} /> : <Pointer size={15} />}
          </button>
        </>
      )}
      {MOD_KEYS.filter((m) => Boolean(desktop) || !m.desktopOnly).map((m) => (
        <button key={m.key} className={`key mod ${mods[m.key] ? 'on' : ''}`} aria-pressed={mods[m.key]}
                onClick={() => toggleMod(m.key)}>{m.label}</button>
      ))}
      {KEYS.map((k) => (
        <button key={k.key} className="key" onClick={() => target.sendSpecialKey(k.key)}>{k.label}</button>
      ))}
      <button className={`key ${fnKeys ? 'on' : ''}`} onClick={() => setFnKeys((v) => !v)}>Fn</button>
      {fnKeys && F_KEYS.map((k) => (
        <button key={k} className="key" onClick={() => target.sendSpecialKey(k)}>{k}</button>
      ))}
    </div>
  );
}
