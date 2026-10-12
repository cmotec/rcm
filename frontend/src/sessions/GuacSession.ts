import { wsUrl } from '../api/client';
import type { SessionInfo } from '../api/types';
import { saveBlob } from '../util/download';
import { BaseSession } from './base';
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
  remoteClipboard = '';
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
    this.setStatus(this.reconnectAttempts ? 'reconnecting' : 'connecting', 'Connecting…');

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
      keyboard.onkeydown = (keysym) => {
        client.sendKeyEvent(1, keysym);
        this.mirrorKey(1, keysym);
        return false;
      };
      keyboard.onkeyup = (keysym) => {
        client.sendKeyEvent(0, keysym);
        this.mirrorKey(0, keysym);
        // a key typed on the soft keyboard releases modifiers latched on the touch key bar
        if (this.latchedKeysyms.size && !MODIFIER_VALUES.has(keysym)) this.releaseLatched();
      };
      this.keyboard = keyboard;
    }

    // --- display scaling
    display.onresize = () => this.rescale();

    // --- clipboard (remote → local)
    client.onclipboard = (stream, mimetype) => {
      if (this.settings.disable_copy || !/^text\//.test(mimetype)) return;
      const reader = new Guacamole.StringReader(stream);
      let text = '';
      reader.ontext = (t) => (text += t);
      reader.onend = () => {
        this.remoteClipboard = text;
        navigator.clipboard?.writeText(text).catch(() => undefined);
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
      if (state === 1 || state === 2) this.setStatus(this.snapshot.status === 'reconnecting' ? 'reconnecting' : 'connecting', 'Connecting…');
      else if (state === 3) {
        this.connectionEstablished();
        this.rescale();
        this.sendSizeNow();
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

    const dpr = window.devicePixelRatio || 1;
    const params = new URLSearchParams({
      ticket,
      width: String(Math.round(cssWidth * dpr)),
      height: String(Math.round(cssHeight * dpr)),
      dpi: String(Math.round(96 * dpr)),
      tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
    });
    for (const a of Guacamole.AudioPlayer.getSupportedTypes()) params.append('audio', a);
    for (const i of ['image/png', 'image/jpeg', 'image/webp']) params.append('image', i);
    this.lastSentSize = { w: Math.round(cssWidth * dpr), h: Math.round(cssHeight * dpr) };
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

  private sendSizeNow(): void {
    const client = this.client;
    if (!client || this.settings.server_resize === false) return;
    const dpr = window.devicePixelRatio || 1;
    const w = Math.round(this.element.clientWidth * dpr);
    const h = Math.round(this.element.clientHeight * dpr);
    if (w < 64 || h < 64) return;
    if (Math.abs(w - this.lastSentSize.w) < 8 && Math.abs(h - this.lastSentSize.h) < 8) return;
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
