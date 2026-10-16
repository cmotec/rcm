import {
  ClipboardIcon,
  Columns2,
  ExternalLink,
  GripVertical,
  Keyboard,
  LoaderCircle,
  Maximize,
  Maximize2,
  Minimize2,
  Minus,
  PanelTop,
  Radio,
  RotateCw,
  ShieldAlert,
  SquareArrowOutUpRight,
  Upload,
  X,
} from 'lucide-react';
import { type ReactNode, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { ProtocolIcon } from '../components/common/icons';
import { Modal } from '../components/common/Modal';
import { GuacSession } from '../sessions/GuacSession';
import { TerminalSession } from '../sessions/TerminalSession';
import { closeSessionById, getController, openInNewWindow } from '../sessions/registry';
import { useWorkspace } from '../state/workspace';
import { formatBytes } from '../util/download';
import { useMediaQuery } from '../util/useMediaQuery';
import { enterAppFullscreen, exitAppFullscreen } from './appFullscreen';
import { MobileKeyBar } from './MobileKeyBar';
import { dockToTab, floatSession, focusSession, placementOf } from './model';
import { SESSION_MIME, useSessionState } from './useSession';

/** Borrows the session's persistent element; parks it when unmounted so the session survives. */
function SessionSlot({ sessionId }: { sessionId: string }) {
  const ref = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const ctrl = getController(sessionId);
    const slot = ref.current;
    if (!ctrl || !slot) return undefined;
    slot.appendChild(ctrl.element);
    return () => {
      if (ctrl.element.parentElement === slot) document.getElementById('session-parking')?.appendChild(ctrl.element);
    };
  }, [sessionId]);
  return <div ref={ref} className="session-slot" />;
}

function StatusOverlay({ sessionId }: { sessionId: string }) {
  const s = useSessionState(sessionId);
  if (s.status === 'connected') return null;
  const ctrl = getController(sessionId);
  const meta = useWorkspace.getState().sessions[sessionId];
  const busy = s.status === 'connecting' || s.status === 'reconnecting';
  return (
    <div className={`session-overlay ${s.status}`}>
      <div className="overlay-card">
        {busy && <LoaderCircle className="spin" size={26} />}
        {meta && <div className="overlay-name"><ProtocolIcon protocol={meta.protocol} /> {meta.name}</div>}
        <div className="overlay-title">
          {s.status === 'connecting' && 'Connecting…'}
          {s.status === 'reconnecting' && 'Reconnecting…'}
          {s.status === 'disconnected' && 'Disconnected'}
          {s.status === 'error' && 'Connection failed'}
          {s.status === 'closed' && 'Closed'}
        </div>
        {s.message && <div className="overlay-msg">{s.message}</div>}
        {busy && (
          <div className="overlay-actions">
            <button onClick={() => void closeSessionById(sessionId)}>Cancel</button>
          </div>
        )}
        {(s.status === 'disconnected' || s.status === 'error') && (
          <div className="overlay-actions">
            <button className="primary" onClick={() => void ctrl?.reconnect(true)}><RotateCw size={14} /> Reconnect</button>
            <button onClick={() => void closeSessionById(sessionId)}>Close</button>
          </div>
        )}
      </div>
    </div>
  );
}

function ClipboardDialog({ ctrl, onClose }: { ctrl: GuacSession | TerminalSession; onClose: () => void }) {
  const remote = ctrl instanceof GuacSession ? ctrl.remoteClipboard : ctrl.getSelection();
  const [text, setText] = useState('');
  return (
    <Modal title="Clipboard" onClose={onClose} width={520}
           footer={<>
             <button onClick={onClose}>Close</button>
             <button className="primary" disabled={!text} onClick={() => {
               if (ctrl instanceof GuacSession) ctrl.sendClipboard(text);
               else ctrl.paste(text);
               onClose();
             }}>{ctrl instanceof GuacSession ? 'Send to remote clipboard' : 'Paste into terminal'}</button>
           </>}>
      <label className="field"><span>{ctrl instanceof GuacSession ? 'Remote clipboard' : 'Selected text'}</span>
        <textarea readOnly rows={4} value={remote} onFocus={(e) => e.currentTarget.select()} />
      </label>
      <label className="field"><span>Text to send</span>
        <textarea rows={5} value={text} onChange={(e) => setText(e.target.value)} autoFocus />
      </label>
    </Modal>
  );
}

function GuacTransfers({ ctrl }: { ctrl: GuacSession }) {
  const [, force] = useState(0);
  useEffect(() => ctrl.onTransfers(() => force((n) => n + 1)), [ctrl]);
  if (!ctrl.transfers.length) return null;
  return (
    <div className="guac-transfers">
      {ctrl.transfers.map((t) => (
        <div key={t.id} className={`transfer ${t.state}`}>
          <span className="transfer-name">{t.direction === 'upload' ? '↑' : '↓'} {t.name}</span>
          <span className="transfer-meta">{t.state === 'failed' ? t.error : `${formatBytes(t.transferred)}${t.size ? ` / ${formatBytes(t.size)}` : ''}`}</span>
        </div>
      ))}
      <button className="link-btn" onClick={() => ctrl.clearFinishedTransfers()}>Clear</button>
    </div>
  );
}

export type FrameMode = 'tab' | 'split' | 'float';

interface FrameProps {
  sessionId: string;
  mode: FrameMode;
  floatControls?: ReactNode;
  onTitlePointerDown?: (e: React.PointerEvent) => void;
  onTitleDoubleClick?: () => void;
  /** Alone in its own browser window: no layout controls (split, tab, floating, new window). */
  standalone?: boolean;
}

export function SessionFrame({ sessionId, mode, floatControls, onTitlePointerDown, onTitleDoubleClick, standalone }: FrameProps) {
  const meta = useWorkspace((s) => s.sessions[sessionId]);
  const focused = useWorkspace((s) => s.layout.focusedSessionId === sessionId);
  // "type in all panes" is on for the tab this pane belongs to
  const broadcasting = useWorkspace((s) => {
    const p = placementOf(s.layout, sessionId);
    return p.where === 'tab' && p.split && Boolean(s.broadcast[p.tabId]);
  });
  const update = useWorkspace((s) => s.update);
  const fullscreen = useWorkspace((s) => s.fullscreenSessionId === sessionId);
  const state = useSessionState(sessionId);
  const [clipboard, setClipboard] = useState(false);
  const ctrl = getController(sessionId);
  const upload = useRef<HTMLInputElement>(null);
  const coarsePointer = useMediaQuery('(pointer: coarse)');
  if (!meta || !ctrl) return null;
  const unencrypted = meta.warnings.find((w) => ['unencrypted', 'encryption_not_guaranteed', 'cert_ignored'].includes(w.code));

  const focus = () => {
    update((l) => focusSession(l, sessionId));
    ctrl.focus();
  };

  const popOut = () => {
    const ws = document.querySelector('.workspace-body')?.getBoundingClientRect();
    const w = Math.min(960, (ws?.width ?? 1200) * 0.6);
    const h = Math.min(640, (ws?.height ?? 800) * 0.6);
    update((l) => floatSession(l, sessionId, { x: 40 + Math.random() * 40, y: 30 + Math.random() * 30, w, h }));
  };

  return (
    <div className={`session-frame mode-${mode} ${focused ? 'focused' : ''} ${fullscreen ? 'app-fullscreen' : ''}`}
         onMouseDownCapture={focus} data-leaf={mode !== 'float' ? sessionId : undefined}>
      {fullscreen && (
        <button className="fullscreen-exit" title="Exit fullscreen" onClick={() => { exitAppFullscreen(); ctrl.focus(); }}>
          <Minimize2 size={16} />
        </button>
      )}
      <div className="frame-head" onPointerDown={onTitlePointerDown} onDoubleClick={onTitleDoubleClick}>
        {!standalone && (
          <span className="drag-grip" draggable title="Drag onto the workspace to split, onto the tab bar to dock"
                onDragStart={(e) => {
                  e.dataTransfer.setData(SESSION_MIME, sessionId);
                  e.dataTransfer.effectAllowed = 'move';
                }} onPointerDown={(e) => e.stopPropagation()}>
            <GripVertical size={14} />
          </span>
        )}
        <ProtocolIcon protocol={meta.protocol} size={14} />
        <span className="frame-title" title={`${meta.name} — ${meta.host ?? ''}`}>{meta.name}</span>
        <span className={`status-dot ${state.status}`} title={state.message ?? state.status} />
        {unencrypted && <span className="badge warn" title={unencrypted.message}><ShieldAlert size={12} /> {unencrypted.code === 'cert_ignored' ? 'cert not verified' : 'unencrypted'}</span>}
        {broadcasting && <span className="badge broadcast" title="Keyboard input goes to all panes of this tab"><Radio size={11} /> all panes</span>}
        {state.detail && <span className="frame-detail">{state.detail}</span>}
        <span className="spacer" />
        <div className="frame-actions" onPointerDown={(e) => e.stopPropagation()}>
          {ctrl instanceof GuacSession && (
            <>
              <button className="icon-btn" title="Send Ctrl+Alt+Del" onClick={() => ctrl.sendCtrlAltDel()}><Keyboard size={14} /></button>
              {ctrl.uploadsAllowed && (
                <button className="icon-btn" title="Upload files to the redirected drive" onClick={() => upload.current?.click()}>
                  <Upload size={14} />
                </button>
              )}
            </>
          )}
          {(ctrl instanceof GuacSession || ctrl instanceof TerminalSession) && (
            <button className="icon-btn" title="Clipboard" onClick={() => setClipboard(true)}><ClipboardIcon size={14} /></button>
          )}
          <button className="icon-btn" title="Reconnect" onClick={() => void ctrl.reconnect(true)}><RotateCw size={14} /></button>
          {ctrl.info.kind !== 'files' && (
            <button className="icon-btn" title="Fullscreen (whole app, no menus)" onClick={() => { enterAppFullscreen(sessionId); ctrl.focus(); }}>
              <Maximize size={14} />
            </button>
          )}
          {!standalone && mode === 'split' && (
            <button className="icon-btn" title="Move to own tab" onClick={() => update((l) => dockToTab(l, sessionId))}>
              <PanelTop size={14} />
            </button>
          )}
          {!standalone && mode !== 'float' && (
            <button className="icon-btn" title="Pop out into a floating window" onClick={popOut}>
              <SquareArrowOutUpRight size={14} />
            </button>
          )}
          {!standalone && ctrl.info.kind !== 'files' && (
            <button className="icon-btn" title="Open in a new browser window (e.g. on another monitor)"
                    onClick={() => openInNewWindow(sessionId)}>
              <ExternalLink size={14} />
            </button>
          )}
          {floatControls}
          <button className="icon-btn danger" title="Disconnect" onClick={() => void closeSessionById(sessionId)}><X size={14} /></button>
        </div>
      </div>
      <div className="frame-body">
        <SessionSlot sessionId={sessionId} />
        <StatusOverlay sessionId={sessionId} />
        {ctrl instanceof GuacSession && <GuacTransfers ctrl={ctrl} />}
      </div>
      {/* below the session: right above the on-screen keyboard while typing */}
      {coarsePointer && (ctrl instanceof GuacSession || ctrl instanceof TerminalSession) && (
        <MobileKeyBar target={ctrl} desktop={ctrl instanceof GuacSession ? ctrl : undefined} />
      )}
      {ctrl instanceof GuacSession && (
        <input ref={upload} type="file" multiple hidden onChange={(e) => {
          Array.from(e.target.files ?? []).forEach((f) => ctrl.uploadFile(f));
          e.target.value = '';
        }} />
      )}
      {clipboard && (ctrl instanceof GuacSession || ctrl instanceof TerminalSession) && (
        <ClipboardDialog ctrl={ctrl} onClose={() => setClipboard(false)} />
      )}
    </div>
  );
}

export const FloatIcons = { Minus, Maximize2, Minimize2, PanelTop, Columns2 };
