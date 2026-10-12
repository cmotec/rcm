import { createRoot, type Root } from 'react-dom/client';
import type { SessionInfo } from '../api/types';
import { FileBrowser } from '../components/files/FileBrowser';
import { BaseSession } from './base';

/** SFTP / FTP / FTPS file manager session (REST based, no persistent socket in the browser). */
export class FileSession extends BaseSession {
  private root: Root | null = null;

  constructor(info: SessionInfo) {
    super(info);
    this.setStatus('connected');
  }

  get supportsChmod(): boolean {
    return Boolean(this.info.client_settings.supports_chmod);
  }

  get startDirectory(): string | null {
    return (this.info.client_settings.start_directory as string | null) ?? null;
  }

  get defaultShowHidden(): boolean {
    return Boolean(this.info.client_settings.show_hidden);
  }

  /** Mark the session disconnected after the server reported it (e.g. idle timeout). */
  markDisconnected(message: string): void {
    this.setStatus('disconnected', message);
  }

  markConnected(): void {
    this.connectionEstablished();
  }

  protected start(): void {
    const mount = document.createElement('div');
    mount.className = 'file-browser-mount';
    this.element.appendChild(mount);
    this.root = createRoot(mount);
    this.root.render(<FileBrowser session={this} />);
  }

  protected onResize(): void {
    /* layout is CSS driven */
  }

  protected connectWithTicket(): void {
    /* file sessions reconnect without a ticket */
  }

  protected teardown(): void {
    /* nothing persistent in the browser */
  }

  dispose(): void {
    const r = this.root;
    this.root = null;
    if (r) queueMicrotask(() => r.unmount());
    super.dispose();
  }
}
