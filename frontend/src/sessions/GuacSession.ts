import { wsUrl } from '../api/client';
import type { SessionInfo } from '../api/types';
import { dialogs } from '../state/dialogs';
import { saveBlob } from '../util/download';
import { BaseSession } from './base';
import { sessionPrefs } from './prefs';
import { MODIFIER_KEYSYMS, type Modifiers, type SpecialKey, type SpecialKeyTarget, specialKeysym } from './specialKeys';
import Guacamole from './guac';
import type { GuacTypes } from './guac';

// Guacamole status codes after which reconnecting cannot help.
const FATAL_CODES = new Set([0x0300, 0x0301, 0x0303, 0x0209]);
// The remote side ended the session normally (logoff / disconnect): close the window.
const ENDED_CODES = new Set([0x0000, 0x020b]);
const RESIZE_DEBOUNCE_MS = 200;
const MODIFIER_VALUES = new Set(Object.values(MODIFIER_KEYSYMS));

export interface FileTransfer {
  id: string;
  name: string;
  direction: 'upload' | 'download';
  size: number | null;
  transferred: number;
  state: 'running' | 'done' | 'failed';
  error?: string;
}

/** RDP / VNC (and SSH via guacd) session rendered by guacamole-common-js. */
const IS_MAC = /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent);
/** Meta_L/R and Super_L/R (the Command keys) → Control_L/R. */
const COMMAND_TO_CTRL = new Map([[0xffe7, 0xffe3], [0xffe8, 0xffe4], [0xffeb, 0xffe3], [0xffec, 0xffe4]]);
/** Shift_L/R, Alt_L/R, ISO_Level3_Shift (Option may arrive as either). */
const COMPOSE_MODIFIERS = new Set([0xffe1, 0xffe2, 0xffe9, 0xffea, 0xfe03]);

/** A keysym that produces a character (Latin-1 or Unicode), as opposed to a function key. */
function isCharacter(keysym: number): boolean {
  return (keysym >= 0x20 && keysym <= 0x7e) || (keysym >= 0xa0 && keysym <= 0xff) || keysym >= 0x01000000;
}

/**
 * Remote desktop size for a slot of css pixels: device pixels for sharp HiDPI, but within what RDP
 * hosts accept (larger requests make many hosts fall back to 1024×768).
 */
const MAX_REMOTE = { w: 4096, h: 2048 };
function remoteSize(cssWidth: number, cssHeight: number): { w: number; h: number; dpi: number } {
  const dpr = window.devicePixelRatio || 1;
  const factor = Math.max(1, Math.min(dpr, MAX_REMOTE.w / Math.max(cssWidth, 1), MAX_REMOTE.h / Math.max(cssHeight, 1)));
  return {
    w: Math.min(MAX_REMOTE.w, Math.round(cssWidth * factor)),
    h: Math.min(MAX_REMOTE.h, Math.round(cssHeight * factor)),
    dpi: Math.round(96 * factor),
  };
}

export class GuacSession extends BaseSession implements SpecialKeyTarget {
  private client: GuacTypes.Client | null = null;
  private keyboard: GuacTypes.Keyboard | null = null;
  private displayHost: HTMLDivElement;
  private resizeTimer: number | null = null;
  private lastSentSize = { w: 0, h: 0 };
  private lastError: GuacTypes.Status | null = null;
  private tearingDown = false;
  private localCursor = false;
  private lastLocalClipboard = '';
  /** Keys currently held down on the remote (from the physical keyboard). */
  private remoteDown = new Set<number>();
  /** Option-composed characters → the modifiers released for them (pressed again on key up). */
  private composed = new Map<number, number[]>();
  /** Guacamole client state (3 = connected) and whether the remote screen has been drawn yet. */
  private guacState = 0;
  private firstFrame = false;
  private firstFrameTimer: number | null = null;
  remoteClipboard = '';
  /** Remote clipboard text the browser has not accepted yet (written on the next user gesture). */
  private pendingLocalCopy: string | null = null;
  /** Ctrl/⌘+V held back until the browser hands over the local clipboard (see holdPaste). */
  private heldPaste: { keysym: number; queued: [0 | 1, number][]; timer: number; refocus: boolean } | null = null;
  transfers: FileTransfer[] = [];
  private transferListeners = new Set<() => void>();
  private pendingTicket: string | null;
  /** Hidden text field that brings up the on-screen keyboard on phones/tablets. */
  private keyInput: HTMLTextAreaElement;

  constructor(info: SessionInfo) {
    super(info);
    this.pendingTicket = info.ticket;
    this.displayHost = document.createElement('div');
    this.displayHost.className = 'guac-display-host';
    this.element.appendChild(this.displayHost);
    this.keyInput = document.createElement('textarea');
    this.keyInput.className = 'guac-key-input';
    this.keyInput.setAttribute('autocapitalize', 'off');
    this.keyInput.setAttribute('autocomplete', 'off');
    this.keyInput.setAttribute('autocorrect', 'off');
    this.keyInput.spellcheck = false;
    this.keyInput.setAttribute('aria-label', 'Keyboard input');
    // Key events bubble to the session element, where Guacamole.Keyboard handles them; keep the field empty.
    this.keyInput.addEventListener('input', () => window.setTimeout(() => (this.keyInput.value = ''), 0));
    this.element.appendChild(this.keyInput);
    if (window.matchMedia('(pointer: coarse)').matches) this.installPinchZoom();
    this.element.addEventListener('focus', () => void this.pushLocalClipboard());
    this.element.addEventListener('dragover', (e) => {
      if (this.uploadsAllowed) e.preventDefault();
    });
    this.element.addEventListener('drop', (e) => {
      if (!this.uploadsAllowed || !e.dataTransfer?.files.length) return;
      e.preventDefault();
      Array.from(e.dataTransfer.files).forEach((f) => this.uploadFile(f));
    });
    this.element.addEventListener('paste', (e) => this.onPaste(e));
    // Copied on the remote while the browser refused the write (no focus / no user gesture yet):
    // retry on the next click, key press or when the window gets the focus back.
    this.element.addEventListener('pointerdown', this.retryCopy, true);
    this.element.addEventListener('keydown', this.retryCopy, true);
    window.addEventListener('focus', this.retryCopy);
  }

  private readonly retryCopy = () => {
    if (this.pendingLocalCopy !== null) this.writeLocalClipboard(this.pendingLocalCopy);
  };

  override dispose(): void {
    window.removeEventListener('focus', this.retryCopy);
    if (this.firstFrameTimer !== null) window.clearTimeout(this.firstFrameTimer);
    if (this.heldPaste) window.clearTimeout(this.heldPaste.timer);
    this.heldPaste = null;
    super.dispose();
  }

  private established(): void {
    if (this.firstFrameTimer !== null) window.clearTimeout(this.firstFrameTimer);
    this.firstFrameTimer = null;
    if (this.snapshot.status !== 'connected') this.connectionEstablished();
  }

  /** Key from the physical keyboard: tracked, sent, and mirrored to broadcast targets. */
  private keyboardKey(pressed: 0 | 1, keysym: number): void {
    if (pressed) this.remoteDown.add(keysym);
    else this.remoteDown.delete(keysym);
    this.client?.sendKeyEvent(pressed, keysym);
    this.mirrorKey(pressed, keysym);
  }

  /** macOS: the Command key acts as Ctrl on the remote (copy/paste with ⌘C/⌘V), unless switched off. */
  private localToRemote(keysym: number): number {
    if (IS_MAC && sessionPrefs.macCommandAsCtrl) return COMMAND_TO_CTRL.get(keysym) ?? keysym;
    return keysym;
  }

  /** Remote → local clipboard. */
  private writeLocalClipboard(text: string): void {
    if (!navigator.clipboard?.writeText) return; // only in secure contexts (HTTPS)
    this.pendingLocalCopy = text;
    navigator.clipboard.writeText(text).then(
      () => {
        if (this.pendingLocalCopy === text) this.pendingLocalCopy = null;
      },
      () => undefined,
    );
  }

  /** Ctrl/⌘+V pressed: move the focus to a text field so the browser fires its paste event there. */
  private holdPaste(keysym: number): void {
    const refocus = document.activeElement !== this.keyInput;
    this.keyInput.value = '';
    this.keyInput.focus({ preventScroll: true });
    // No paste event (empty clipboard, permission denied, …): send the keys anyway.
    const timer = window.setTimeout(() => this.releasePaste(true), 400);
    this.heldPaste = { keysym, queued: [], timer, refocus };
  }

  /** Send the held Ctrl+V (unless dropped) and the keys typed in the meantime, in order. */
  private releasePaste(sendKey: boolean): void {
    const held = this.heldPaste;
    if (!held) return;
    this.heldPaste = null;
    window.clearTimeout(held.timer);
    if (held.refocus && document.activeElement === this.keyInput) this.element.focus({ preventScroll: true });
    this.keyInput.value = '';
    if (sendKey) this.keyboardKey(1, held.keysym);
    for (const [pressed, keysym] of held.queued) {
      if (keysym === held.keysym && !sendKey) continue;
      this.keyboardKey(pressed, keysym);
    }
  }

  /**
   * Paste from the local computer. Files (copied in Finder / Explorer / a file manager) are uploaded
   * to the redirected drive — guacd cannot put files on the remote clipboard. Text goes to the remote
   * clipboard.
   */
  private onPaste(e: ClipboardEvent): void {
    const data = e.clipboardData;
    e.preventDefault(); // never into the hidden text field
    if (!data) {
      this.releasePaste(true);
      return;
    }
    const files = Array.from(data.files ?? []);
    if (files.length) {
      this.releasePaste(false); // files go to the drive, not to the remote clipboard
      if (!this.uploadsAllowed) {
        dialogs.toast(this.settings.drive_enabled
          ? 'Uploads are disabled for this connection.'
          : "Pasting files needs drive redirection: enable 'Drive redirection (file transfer)' in the connection's RDP settings.",
        'warning');
        return;
      }
      dialogs.toast(`Uploading ${files.length} file(s) to the redirected drive (“This PC” on the remote computer)`, 'info');
      files.forEach((f) => this.uploadFile(f));
      return;
    }
    const text = data.getData('text/plain');
    if (text && !this.settings.disable_paste && text !== this.lastLocalClipboard && text !== this.remoteClipboard) {
      this.sendClipboard(text);
      // let guacd announce the new clipboard to the remote before Ctrl+V asks for it
      if (this.heldPaste) {
        window.clearTimeout(this.heldPaste.timer);
        this.heldPaste.timer = window.setTimeout(() => this.releasePaste(true), 120);
      }
      return;
    }
    this.releasePaste(true);
  }

  private get settings() {
    return this.info.client_settings as {
      server_resize?: boolean;
      read_only?: boolean;
      disable_copy?: boolean;
      disable_paste?: boolean;
      drive_enabled?: boolean;
      disable_upload?: boolean;
    };
  }

  get uploadsAllowed(): boolean {
    return Boolean(this.settings.drive_enabled && !this.settings.disable_upload);
  }

  onTransfers(fn: () => void): () => void {
    this.transferListeners.add(fn);
    return () => this.transferListeners.delete(fn);
  }

  private emitTransfers(): void {
    this.transferListeners.forEach((f) => f());
  }

  // ------------------------------------------------------------------ lifecycle

  protected start(width: number, height: number): void {
    if (this.pendingTicket) {
      const t = this.pendingTicket;
      this.pendingTicket = null;
      this.open(t, width, height);
    } else {
      void this.reconnect(true);
    }
  }

  protected connectWithTicket(ticket: string): void {
    this.open(ticket, this.element.clientWidth || 1024, this.element.clientHeight || 768);
  }

  private open(ticket: string, cssWidth: number, cssHeight: number): void {
    this.teardown();
    this.tearingDown = false;
    this.lastError = null;
    this.firstFrame = false;
    this.setStatus(this.reconnectAttempts ? 'reconnecting' : 'connecting', 'Opening the connection…');

    const tunnel = new Guacamole.WebSocketTunnel(wsUrl(this.info.tunnel_path!));
    const client = new Guacamole.Client(tunnel);
    this.client = client;
    const display = client.getDisplay();
    const displayEl = display.getElement();
    displayEl.classList.add('guac-display');
    this.displayHost.replaceChildren(displayEl);

    // --- input
    if (!this.settings.read_only) {
      const mouse = new Guacamole.Mouse(displayEl);
      mouse.onEach(['mousedown', 'mousemove', 'mouseup'], (e) => {
        const ev = e as unknown as { state: GuacTypes.Mouse.State };
        this.element.focus({ preventScroll: true });
        display.showCursor(!this.localCursor);
        client.sendMouseState(ev.state, true);
      });
      mouse.on('mouseout', () => display.showCursor(false));
      if (window.matchMedia('(pointer: coarse)').matches) {
        // Touch, two modes (both devices listen; only the active mode sends):
        // touchpad (default): drag moves the pointer, tap clicks at the pointer, two-finger drag scrolls,
        //   two-finger tap = right click; the zoomed view follows the pointer.
        // direct: tap clicks where you touch, drag = move with the button held.
        const pad = new Guacamole.Mouse.Touchpad(displayEl);
        pad.onEach(['mousedown', 'mousemove', 'mouseup'], (e) => {
          if (this.pointerMode !== 'touchpad') return;
          const state = (e as unknown as { state: GuacTypes.Mouse.State }).state;
          display.showCursor(true);
          client.sendMouseState(state, true);
          this.followPointer(state.x, state.y);
        });
        const screen = new Guacamole.Mouse.Touchscreen(displayEl);
        screen.onEach(['mousedown', 'mousemove', 'mouseup'], (e) => {
          if (this.pointerMode !== 'touchscreen') return;
          client.sendMouseState((e as unknown as { state: GuacTypes.Mouse.State }).state, true);
        });
      }
      display.oncursor = (canvas, x, y) => {
        this.localCursor = (mouse as unknown as { setCursor(c: HTMLCanvasElement, x: number, y: number): boolean })
          .setCursor(canvas, x, y);
      };
      const keyboard = new Guacamole.Keyboard(this.element);
      keyboard.onkeydown = (raw) => {
        const keysym = this.localToRemote(raw);
        if (this.heldPaste) {
          if (raw !== this.heldPaste.keysym) this.heldPaste.queued.push([1, keysym]);
          return false;
        }
        const pasteShortcut = (raw === 0x76 || raw === 0x56) && (keyboard.modifiers.ctrl || keyboard.modifiers.meta);
        if (pasteShortcut && (!this.settings.disable_paste || this.uploadsAllowed)) {
          // Hold the V back and let the browser paste (onPaste): the local clipboard reaches the
          // remote first, then Ctrl+V pastes it there. Returning true keeps the browser's paste action.
          this.holdPaste(raw);
          return true;
        }
        if (IS_MAC && keyboard.modifiers.alt && !keyboard.modifiers.ctrl && !keyboard.modifiers.meta && isCharacter(keysym)) {
          // macOS Option (+ Shift) composes characters (Shift+Option+7 = "\" on a German Mac). Send the
          // character itself without Option/Shift held: guacd types it with the remote layout's keys.
          const lifted = [...this.remoteDown].filter((k) => COMPOSE_MODIFIERS.has(k));
          lifted.forEach((k) => this.keyboardKey(0, k));
          this.composed.set(raw, lifted);
          this.keyboardKey(1, keysym);
          return false;
        }
        this.keyboardKey(1, keysym);
        return false;
      };
      keyboard.onkeyup = (raw) => {
        const keysym = this.localToRemote(raw);
        if (this.heldPaste) {
          this.heldPaste.queued.push([0, keysym]);
          return;
        }
        const lifted = this.composed.get(raw);
        if (lifted) {
          this.composed.delete(raw);
          this.keyboardKey(0, keysym);
          // modifiers the user still holds go down again (e.g. typing several "\" in a row)
          const held = (keyboard as unknown as { pressed: Record<number, boolean> }).pressed;
          lifted.filter((k) => held[k]).forEach((k) => this.keyboardKey(1, k));
          return;
        }
        this.keyboardKey(0, keysym);
        // a key typed on the soft keyboard releases modifiers latched on the touch key bar
        if (this.latchedKeysyms.size && !MODIFIER_VALUES.has(keysym)) this.releaseLatched();
      };
      this.keyboard = keyboard;
    }

    // --- display scaling
    display.onresize = () => {
      this.rescale();
      // the remote screen has a size: it is drawing, hide the loading overlay
      if (!this.firstFrame && display.getWidth() > 0) {
        this.firstFrame = true;
        if (this.client === client && this.guacState === 3) this.established();
        // Some hosts start at their default size (1024×768) and accept the window size only once the
        // display-control channel is up: request it again shortly after the first frame.
        const { w, h } = this.lastSentSize;
        if (Math.abs(display.getWidth() - w) > 8 || Math.abs(display.getHeight() - h) > 8) {
          window.setTimeout(() => {
            if (this.client === client) this.sendSizeNow(true);
          }, 1500);
        }
      }
    };

    // --- clipboard (remote → local)
    client.onclipboard = (stream, mimetype) => {
      if (this.settings.disable_copy || !/^text\//.test(mimetype)) return;
      const reader = new Guacamole.StringReader(stream);
      let text = '';
      reader.ontext = (t) => (text += t);
      reader.onend = () => {
        this.remoteClipboard = text;
        this.writeLocalClipboard(text);
      };
    };

    // --- file downloads from the redirected drive
    client.onfile = (stream, mimetype, filename) => {
      const tr: FileTransfer = { id: crypto.randomUUID(), name: filename, direction: 'download', size: null,
        transferred: 0, state: 'running' };
      this.transfers = [...this.transfers, tr];
      this.emitTransfers();
      const reader = new Guacamole.BlobReader(stream, mimetype);
      reader.onprogress = (length) => {
        tr.transferred += length;
        stream.sendAck('Received', Guacamole.Status.Code.SUCCESS);
        this.emitTransfers();
      };
      reader.onend = () => {
        tr.state = 'done';
        saveBlob(reader.getBlob(), filename);
        this.emitTransfers();
      };
      stream.sendAck('Ready', Guacamole.Status.Code.SUCCESS);
    };

    // --- state
    client.onerror = (status) => {
      this.lastError = status;
    };
    client.onstatechange = (state) => {
      if (this.client !== client) return;
      this.guacState = state;
      const busy = this.snapshot.status === 'reconnecting' ? 'reconnecting' : 'connecting';
      if (state === 1) this.setStatus(busy, 'Opening the connection…');
      else if (state === 2) this.setStatus(busy, `Connecting to ${this.info.effective_host ?? 'the remote host'}…`);
      else if (state === 3) {
        this.rescale();
        this.sendSizeNow();
        if (this.firstFrame) {
          this.established();
        } else {
          // guacd is connected, the remote screen is not drawn yet (e.g. Windows still signing in)
          this.setStatus(busy, this.info.protocol === 'rdp' ? 'Signing in…' : 'Waiting for the remote screen…');
          if (this.firstFrameTimer !== null) window.clearTimeout(this.firstFrameTimer);
          this.firstFrameTimer = window.setTimeout(() => {
            if (this.client === client && this.guacState === 3) this.established();
          }, 20_000);
        }
      } else if (state === 5 && !this.tearingDown && !this.userClosed) {
        const err = this.lastError;
        const message = err?.message || 'Disconnected';
        // No error = guacd closed the session cleanly; network drops always report a tunnel error.
        if (!err || ENDED_CODES.has(err.code)) this.ended(message);
        else if (err && FATAL_CODES.has(err.code)) this.setStatus('error', message);
        else this.scheduleReconnect(message);
      }
    };
    tunnel.onerror = (status) => {
      this.lastError = this.lastError ?? status;
    };

    const size = remoteSize(cssWidth, cssHeight);
    const params = new URLSearchParams({
      ticket,
      width: String(size.w),
      height: String(size.h),
      dpi: String(size.dpi),
      tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
    });
    for (const a of Guacamole.AudioPlayer.getSupportedTypes()) params.append('audio', a);
    for (const i of ['image/png', 'image/jpeg', 'image/webp']) params.append('image', i);
    this.lastSentSize = { w: size.w, h: size.h };
    client.connect(params.toString());
  }

  protected teardown(): void {
    this.tearingDown = true;
    if (this.resizeTimer !== null) window.clearTimeout(this.resizeTimer);
    if (this.keyboard) {
      this.keyboard.onkeydown = null;
      this.keyboard.onkeyup = null;
      this.keyboard.reset();
    }
    this.keyboard = null;
    this.remoteDown.clear();
    this.composed.clear();
    if (this.client) {
      const c = this.client;
      this.client = null;
      c.onstatechange = null;
      try {
        c.disconnect();
      } catch {
        /* ignore */
      }
    }
  }

  // ------------------------------------------------------------------ sizing

  protected onResize(): void {
    this.rescale();
    if (this.resizeTimer !== null) window.clearTimeout(this.resizeTimer);
    this.resizeTimer = window.setTimeout(() => this.sendSizeNow(), RESIZE_DEBOUNCE_MS);
  }

  private sendSizeNow(force = false): void {
    const client = this.client;
    if (!client || this.settings.server_resize === false) return;
    const { w, h } = remoteSize(this.element.clientWidth, this.element.clientHeight);
    if (w < 64 || h < 64) return;
    if (!force && Math.abs(w - this.lastSentSize.w) < 8 && Math.abs(h - this.lastSentSize.h) < 8) return;
    this.lastSentSize = { w, h };
    client.sendSize(w, h);
  }

  /** Fit the remote desktop into the available space (visual scaling only). */
  private rescale(): void {
    const client = this.client;
    if (!client) return;
    const display = client.getDisplay();
    const dw = display.getWidth();
    const dh = display.getHeight();
    const w = this.element.clientWidth;
    const h = this.element.clientHeight;
    if (!dw || !dh || !w || !h) return;
    const scale = Math.min(w / dw, h / dh) * this.zoom;
    display.scale(scale);
    this.displayHost.classList.toggle('zoomed', this.zoom > 1);
    const serverResize = this.settings.server_resize !== false;
    this.setDetail(`Remote ${dw}×${dh} · ${Math.round(scale * 100)} % ${this.zoom > 1 ? `(zoom ${this.zoom.toFixed(1)}×)`
      : serverResize ? '(auto-resize)' : '(scaled to fit)'}`);
  }

  // ------------------------------------------------------------------ touch: zoom & pointer mode

  /** Zoom on top of "fit to window" (1 = fit). The remote resolution does not change. */
  private zoom = 1;
  pointerMode: 'touchpad' | 'touchscreen' =
    localStorage.getItem('touch.pointer') === 'touchscreen' ? 'touchscreen' : 'touchpad';

  setPointerMode(mode: 'touchpad' | 'touchscreen'): void {
    this.pointerMode = mode;
    localStorage.setItem('touch.pointer', mode);
  }

  /** Zoom keeping the content under (anchorX, anchorY) — viewport coordinates — in place. */
  private setZoomAt(zoom: number, anchorX: number, anchorY: number): void {
    const host = this.displayHost;
    const rect = host.getBoundingClientRect();
    const ax = anchorX - rect.left;
    const ay = anchorY - rect.top;
    const rx = (host.scrollLeft + ax) / Math.max(1, host.scrollWidth);
    const ry = (host.scrollTop + ay) / Math.max(1, host.scrollHeight);
    this.zoom = Math.min(5, Math.max(1, zoom));
    this.rescale();
    host.scrollLeft = rx * host.scrollWidth - ax;
    host.scrollTop = ry * host.scrollHeight - ay;
  }

  /**
   * Two-finger pinch zooms (and two-finger movement pans while pinching). Listens in the capture
   * phase on the display host: a two-finger tap or plain two-finger drag still reaches the touchpad
   * (right click / scroll); once the finger distance changes noticeably the gesture becomes a zoom and
   * is no longer passed to the remote pointer.
   */
  private installPinchZoom(): void {
    const host = this.displayHost;
    let start: { dist: number; zoom: number; mx: number; my: number } | null = null;
    let pinching = false;
    const metrics = (t: TouchList) => {
      const [a, b] = [t[0], t[1]];
      return {
        dist: Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY),
        mx: (a.clientX + b.clientX) / 2,
        my: (a.clientY + b.clientY) / 2,
      };
    };
    host.addEventListener('touchstart', (e) => {
      if (e.touches.length === 2) {
        const m = metrics(e.touches);
        start = { dist: Math.max(1, m.dist), zoom: this.zoom, mx: m.mx, my: m.my };
        pinching = false;
      } else if (e.touches.length > 2) {
        start = null;
      }
    }, { capture: true, passive: true });
    host.addEventListener('touchmove', (e) => {
      if (!start || e.touches.length !== 2) return;
      const m = metrics(e.touches);
      if (!pinching && Math.abs(m.dist - start.dist) < 24) return; // not (yet) a pinch: leave to the touchpad
      pinching = true;
      e.stopPropagation();
      e.preventDefault();
      this.setZoomAt(start.zoom * (m.dist / start.dist), m.mx, m.my);
      // pan with the finger midpoint
      host.scrollLeft -= m.mx - start.mx;
      host.scrollTop -= m.my - start.my;
      start = { ...start, mx: m.mx, my: m.my };
    }, { capture: true, passive: false });
    // Finger-lift events always reach the touchpad so its finger count resets; a pinch takes longer
    // than its 250 ms tap window, so it is not mistaken for a (right) click.
    const end = (e: TouchEvent) => {
      if (e.touches.length < 2) start = null;
      if (e.touches.length === 0) pinching = false;
    };
    host.addEventListener('touchend', end, { capture: true });
    host.addEventListener('touchcancel', end, { capture: true });
  }

  /** When zoomed in, scroll so the pointer stays visible (x/y in display-element pixels). */
  private followPointer(x: number, y: number): void {
    if (this.zoom <= 1) return;
    const host = this.displayHost;
    const el = host.firstElementChild as HTMLElement | null;
    const px = (el?.offsetLeft ?? 0) + x;
    const py = (el?.offsetTop ?? 0) + y;
    const margin = 48;
    if (px < host.scrollLeft + margin) host.scrollLeft = px - margin;
    else if (px > host.scrollLeft + host.clientWidth - margin) host.scrollLeft = px - host.clientWidth + margin;
    if (py < host.scrollTop + margin) host.scrollTop = py - margin;
    else if (py > host.scrollTop + host.clientHeight - margin) host.scrollTop = py - host.clientHeight + margin;
  }

  // ------------------------------------------------------------------ clipboard & keys

  private async pushLocalClipboard(): Promise<void> {
    if (this.settings.disable_paste || !this.client || !navigator.clipboard?.readText) return;
    try {
      const text = await navigator.clipboard.readText();
      if (text && text !== this.lastLocalClipboard && text !== this.remoteClipboard) this.sendClipboard(text);
    } catch {
      /* permission denied: the clipboard dialog remains available */
    }
  }

  sendClipboard(text: string): void {
    if (!this.client || this.settings.disable_paste) return;
    const stream = this.client.createClipboardStream('text/plain');
    const writer = new Guacamole.StringWriter(stream);
    writer.sendText(text);
    writer.sendEnd();
    this.lastLocalClipboard = text;
  }

  // ------------------------------------------------------------------ touch key bar

  private latchedKeysyms = new Set<number>();
  private onLatchedConsumed: (() => void) | null = null;

  /** Press the latched modifiers now; they are released after the next key. */
  setLatchedModifiers(mods: Modifiers, onConsumed: () => void): void {
    const wanted = new Set(
      (Object.keys(MODIFIER_KEYSYMS) as (keyof Modifiers)[]).filter((k) => mods[k]).map((k) => MODIFIER_KEYSYMS[k]),
    );
    for (const k of this.latchedKeysyms) if (!wanted.has(k)) this.keyEvent(0, k);
    for (const k of wanted) if (!this.latchedKeysyms.has(k)) this.keyEvent(1, k);
    this.latchedKeysyms = wanted;
    this.onLatchedConsumed = onConsumed;
  }

  private releaseLatched(): void {
    for (const k of this.latchedKeysyms) this.keyEvent(0, k);
    this.latchedKeysyms = new Set();
    const cb = this.onLatchedConsumed;
    this.onLatchedConsumed = null;
    cb?.();
  }

  sendSpecialKey(key: SpecialKey): void {
    const keysym = specialKeysym(key);
    this.keyEvent(1, keysym);
    this.keyEvent(0, keysym);
    this.releaseLatched();
  }

  /** Key event to this session and, with keyboard broadcast on, to the other desktops of the tab. */
  private keyEvent(pressed: 0 | 1, keysym: number): void {
    this.injectKey(pressed, keysym);
    this.mirrorKey(pressed, keysym);
  }

  /** Open the device's on-screen keyboard (phones/tablets). */
  showKeyboard(): void {
    this.keyInput.focus({ preventScroll: true });
  }

  /** Keyboard broadcast: repeat a key event in the other desktop sessions of the same tab. */
  private mirrorKey(pressed: 0 | 1, keysym: number): void {
    for (const target of BaseSession.broadcastTargets(this.id)) {
      if (target instanceof GuacSession) target.injectKey(pressed, keysym);
    }
  }

  injectKey(pressed: 0 | 1, keysym: number): void {
    if (!this.settings.read_only) this.client?.sendKeyEvent(pressed, keysym);
  }

  sendKeys(keysyms: number[]): void {
    const c = this.client;
    if (!c) return;
    keysyms.forEach((k) => c.sendKeyEvent(1, k));
    [...keysyms].reverse().forEach((k) => c.sendKeyEvent(0, k));
  }

  sendCtrlAltDel(): void {
    this.sendKeys([0xffe3, 0xffe9, 0xffff]);
  }

  // ------------------------------------------------------------------ drive upload

  uploadFile(file: File): void {
    const client = this.client;
    if (!client || !this.uploadsAllowed) return;
    const tr: FileTransfer = { id: crypto.randomUUID(), name: file.name, direction: 'upload', size: file.size,
      transferred: 0, state: 'running' };
    this.transfers = [...this.transfers, tr];
    this.emitTransfers();
    const stream = client.createFileStream(file.type || 'application/octet-stream', file.name);
    const writer = new Guacamole.BlobWriter(stream);
    writer.onprogress = (_blob, offset) => {
      tr.transferred = offset;
      this.emitTransfers();
    };
    writer.oncomplete = () => {
      writer.sendEnd();
      tr.transferred = file.size;
      tr.state = 'done';
      this.emitTransfers();
    };
    writer.onerror = (_blob, _offset, status) => {
      tr.state = 'failed';
      tr.error = status.message || 'upload failed';
      this.emitTransfers();
    };
    writer.sendBlob(file);
  }

  clearFinishedTransfers(): void {
    this.transfers = this.transfers.filter((t) => t.state === 'running');
    this.emitTransfers();
  }
}
