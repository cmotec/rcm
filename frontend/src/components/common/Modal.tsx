import { X } from 'lucide-react';
import { type ReactNode, useEffect, useRef } from 'react';

interface ModalProps {
  title: ReactNode;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  width?: number;
  className?: string;
}

export function Modal({ title, onClose, children, footer, width = 520, className }: ModalProps) {
  const ref = useRef<HTMLDivElement>(null);
  // Callers pass inline arrow functions; keep the latest one in a ref so the effect below runs
  // only when the dialog opens. (Re-running it on every render stole focus after each keystroke.)
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    // Respect an autoFocus field inside the dialog; otherwise focus the first control.
    if (!ref.current?.contains(document.activeElement)) {
      ref.current?.querySelector<HTMLElement>('input, select, textarea, button.primary')?.focus();
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        onCloseRef.current();
      }
    };
    window.addEventListener('keydown', onKey, true);
    return () => {
      window.removeEventListener('keydown', onKey, true);
      prev?.focus?.();
    };
  }, []);
  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div ref={ref} className={`modal ${className ?? ''}`} style={{ width }} role="dialog" aria-modal="true">
        <div className="modal-head">
          <h2>{title}</h2>
          <button className="icon-btn" onClick={onClose} aria-label="Close"><X size={16} /></button>
        </div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>
  );
}
