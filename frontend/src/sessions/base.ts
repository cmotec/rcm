import { del, post } from '../api/client';
import type { SessionInfo } from '../api/types';

export type SessionStatus = 'connecting' | 'connected' | 'reconnecting' | 'disconnected' | 'error' | 'closed';

export interface SessionSnapshot {
  status: SessionStatus;
  message: string | null;
  detail: string | null; // e.g. "Remote 1920×1080 · scaled 75 %"
}

type Listener = (s: SessionSnapshot) => void;

/**
 * A live remote session in the browser. Owns a persistent DOM element that layout components
 * borrow (append into their slot) — moving between tab, split and floating window never
 * recreates the remote connection.
 */
export abstract class BaseSession {
  readonly element: HTMLDivElement;
  info: SessionInfo;
  protected snapshot: SessionSnapshot = { status: 'connecting', message: null, detail: null };
  private listeners = new Set<Listener>();
  protected userClosed = false;
  /** Called when the remote side ended the session normally (e.g. `exit`, logoff); the registry closes the window. */
  onEnded: ((reason: string) => void) | null = null;
  protected reconnectAttempts = 0;
  private reconnectTimer: number | null = null;
  private resizeObserver: ResizeObserver;
  private started = false;

  constructor(info: SessionInfo) {
    this.info = info;
    this.element = document.createElement('div');
    this.element.className = `session-root session-${info.kind}`;
    this.element.tabIndex = 0;
    this.element.dataset.sessionId = info.id;
    this.resizeObserver = new ResizeObserver(() => {
      const { clientWidth: w, clientHeight: h } = this.element;
      if (w < 10 || h < 10) return; // parked / hidden
      if (!this.started) {
        this.started = true;
        this.start(w, h);
      } else {
        this.onResize(w, h);
      }
    });
    this.resizeObserver.observe(this.element);
  }

  get id(): string {
    return this.info.id;
  }

  get state(): SessionSnapshot {
    return this.snapshot;
  }

  subscribe(fn: Listener): () => void {
    this.listeners.add(fn);
    fn(this.snapshot);
    return () => this.listeners.delete(fn);
  }

  protected setStatus(status: SessionStatus, message: string | null = null, detail?: string | null): void {
    this.snapshot = { status, message, detail: detail === undefined ? this.snapshot.detail : detail };
    this.listeners.forEach((l) => l(this.snapshot));
  }

  protected setDetail(detail: string | null): void {
    this.snapshot = { ...this.snapshot, detail };
    this.listeners.forEach((l) => l(this.snapshot));
  }

  /** Called once, when the element first becomes visible with a real size. */
  protected abstract start(width: number, height: number): void;

  protected abstract onResize(width: number, height: number): void;

  /** Tear down the local connection objects (tunnel, terminal, …) without closing the server session. */
  protected abstract teardown(): void;

  /** Open the transport again with a fresh ticket. */
  protected abstract connectWithTicket(ticket: string): void;

  get autoReconnect(): boolean {
    return this.info.client_settings.auto_reconnect !== false;
  }

  focus(): void {
    this.element.focus({ preventScroll: true });
  }

  /** Ask the server for a fresh ticket and reconnect (manual or automatic). */
  async reconnect(manual = true): Promise<void> {
    if (this.userClosed) return;
    if (manual) this.reconnectAttempts = 0;
    this.clearReconnectTimer();
    this.setStatus('reconnecting', manual ? 'Reconnecting…' : `Connection lost — reconnecting (attempt ${this.reconnectAttempts + 1})…`);
    this.teardown();
    try {
      const fresh = await post<SessionInfo>(`/api/v1/sessions/${this.id}/reconnect`);
      this.info = { ...this.info, ...fresh, client_settings: this.info.client_settings };
      if (fresh.ticket) this.connectWithTicket(fresh.ticket);
      else this.setStatus('connected');
    } catch (err) {
      this.setStatus('error', err instanceof Error ? err.message : String(err));
    }
  }

  protected scheduleReconnect(reason: string): void {
    if (this.userClosed || !this.autoReconnect || this.reconnectAttempts >= 5) {
      this.setStatus('disconnected', reason);
      return;
    }
    const delays = [1000, 2000, 5000, 10000, 20000];
    const delay = delays[Math.min(this.reconnectAttempts, delays.length - 1)];
    this.reconnectAttempts += 1;
    this.setStatus('reconnecting', `${reason} — retrying in ${Math.round(delay / 1000)} s`);
    this.reconnectTimer = window.setTimeout(() => void this.reconnect(false), delay);
  }

  /** The remote side ended the session normally: no reconnect, the window closes. */
  protected ended(reason: string): void {
    if (this.userClosed) return;
    this.userClosed = true;
    this.setStatus('closed', reason);
    this.onEnded?.(reason);
  }

  protected connectionEstablished(): void {
    this.reconnectAttempts = 0;
    this.setStatus('connected');
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer !== null) {
      window.clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  /** Close the remote session on the server and release everything. */
  async close(): Promise<void> {
    this.userClosed = true;
    this.clearReconnectTimer();
    this.teardown();
    this.setStatus('closed');
    try {
      await del(`/api/v1/sessions/${this.id}`);
    } catch {
      /* already gone */
    }
    this.dispose();
  }

  dispose(): void {
    this.userClosed = true;
    this.clearReconnectTimer();
    this.resizeObserver.disconnect();
    this.teardown();
    this.element.remove();
  }
}
