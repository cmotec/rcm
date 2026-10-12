import { FitAddon } from '@xterm/addon-fit';
import { Terminal } from '@xterm/xterm';
import '@xterm/xterm/css/xterm.css';
import { wsUrl } from '../api/client';
import type { SessionInfo } from '../api/types';
import { BaseSession } from './base';

const THEMES: Record<string, { background: string; foreground: string; cursor?: string }> = {
  dark: { background: '#0f1115', foreground: '#d6dae0', cursor: '#9ecbff' },
  light: { background: '#ffffff', foreground: '#1f2328', cursor: '#0969da' },
  'green-black': { background: '#000000', foreground: '#33ff66' },
  'white-black': { background: '#000000', foreground: '#ffffff' },
  'black-white': { background: '#ffffff', foreground: '#000000' },
};

/** Native SSH terminal (xterm.js ↔ WebSocket ↔ asyncssh). */
export class TerminalSession extends BaseSession {
  private term: Terminal | null = null;
  private fit: FitAddon | null = null;
  private ws: WebSocket | null = null;
  private host: HTMLDivElement;
  private pendingTicket: string | null;
  private encoder = new TextEncoder();
  private resizeTimer: number | null = null;

  constructor(info: SessionInfo) {
    super(info);
    this.pendingTicket = info.ticket;
    this.host = document.createElement('div');
    this.host.className = 'terminal-host';
    this.element.appendChild(this.host);
    this.element.addEventListener('focus', () => this.term?.focus());
  }

  private get settings() {
    return this.info.client_settings as { font_size?: number; color_scheme?: string; scrollback?: number };
  }

  protected start(): void {
    const s = this.settings;
    const term = new Terminal({
      fontFamily: 'ui-monospace, "Cascadia Mono", "JetBrains Mono", Menlo, Consolas, monospace',
      fontSize: s.font_size ?? 14,
      scrollback: s.scrollback ?? 5000,
      cursorBlink: true,
      allowProposedApi: false,
      theme: THEMES[s.color_scheme ?? ''] ?? THEMES.dark,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(this.host);
    fit.fit();
    term.onData((data) => this.send(this.encoder.encode(data)));
    term.onBinary((data) => this.send(Uint8Array.from(data, (c) => c.charCodeAt(0) & 0xff)));
    term.onResize(({ cols, rows }) => this.sendControl({ type: 'resize', cols, rows }));
    this.term = term;
    this.fit = fit;
    if (this.pendingTicket) {
      const t = this.pendingTicket;
      this.pendingTicket = null;
      this.connectWithTicket(t);
    } else {
      void this.reconnect(true);
    }
  }

  protected onResize(): void {
    if (this.resizeTimer !== null) window.clearTimeout(this.resizeTimer);
    this.resizeTimer = window.setTimeout(() => this.fit?.fit(), 60);
  }

  protected connectWithTicket(ticket: string): void {
    const term = this.term;
    if (!term) return;
    this.closeSocket();
    this.setStatus(this.reconnectAttempts ? 'reconnecting' : 'connecting', 'Connecting…');
    const url = `${wsUrl(this.info.tunnel_path!)}?ticket=${encodeURIComponent(ticket)}&cols=${term.cols}&rows=${term.rows}`;
    const ws = new WebSocket(url);
    ws.binaryType = 'arraybuffer';
    this.ws = ws;
    let fatal: string | null = null;
    let exited = false;
    ws.onmessage = (ev) => {
      if (typeof ev.data !== 'string') {
        term.write(new Uint8Array(ev.data as ArrayBuffer));
        return;
      }
      const msg = JSON.parse(ev.data) as { type: string; state?: string; code?: string; message?: string; status?: number | null };
      if (msg.type === 'status' && msg.state === 'connected') {
        this.connectionEstablished();
        this.fit?.fit();
      } else if (msg.type === 'error') {
        fatal = msg.message ?? 'error';
        term.write(`\r\n\x1b[31m${fatal}\x1b[0m\r\n`);
        if (msg.code === 'remote_auth_failed' || msg.code?.startsWith('hostkey')) this.setStatus('error', fatal);
      } else if (msg.type === 'exit') {
        exited = true;
        term.write(`\r\n\x1b[90m[session ended${msg.status !== null && msg.status !== undefined ? ` with status ${msg.status}` : ''}]\x1b[0m\r\n`);
      }
    };
    ws.onclose = (ev) => {
      if (this.ws !== ws || this.userClosed) return;
      this.ws = null;
      if (exited) this.ended('Remote shell exited');
      else if (this.snapshot.status === 'error') return;
      else if (ev.code === 1008) this.setStatus('disconnected', 'Session ticket rejected');
      else this.scheduleReconnect(fatal ?? 'Connection lost');
    };
  }

  private send(data: Uint8Array): void {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(data);
  }

  private sendControl(msg: object): void {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(msg));
  }

  paste(text: string): void {
    this.term?.paste(text);
  }

  getSelection(): string {
    return this.term?.getSelection() ?? '';
  }

  private closeSocket(): void {
    if (this.ws) {
      const ws = this.ws;
      this.ws = null;
      ws.onclose = null;
      ws.close();
    }
  }

  protected teardown(): void {
    this.closeSocket();
  }

  dispose(): void {
    super.dispose();
    this.term?.dispose();
    this.term = null;
  }
}
