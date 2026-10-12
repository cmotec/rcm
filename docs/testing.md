# Testing

## Automated

| Suite | Command | What it covers |
|---|---|---|
| Backend (pytest, 69 tests) | `cd backend && pip install -e ".[test]" && pytest` | real PostgreSQL (temporary instance via `pgserver`) with migrations; crypto & AAD; Guacamole codec; inheritance/resolution rules; DNS search domains; RBAC/ACL; CSRF/Origin; API tokens; idempotency; optimistic locking; credential encryption at rest & audited reveal; OIDC (mocked IdP, real RS256, PKCE, nonce/state); **guacd tunnel against a fake guacd speaking the wire protocol** (handshake values, opcode filtering, tickets, ownership); **native SSH** (host-key confirmation, terminal, resize, wrong password, interactive credentials) against an in-process asyncssh server; **SFTP** and **FTP** file operations against in-process servers; secret sync conflict rules; Infisical/OpenBao HTTP requests; RDM parsing/import; a regression guard that DB transactions commit before responses |
| Frontend unit (vitest) | `cd frontend && npm test` | workspace layout model: tabs, floating, split, collapse, persistence/restore |
| Frontend type check + build | `cd frontend && npm run build` | TypeScript strict mode |
| Browser end-to-end (Playwright) | see [../e2e/README.md](../e2e/README.md) | real UI in Chromium against the real API: login, tree, host-key dialog, live terminal, SFTP/FTP, floating windows, split view, taskbar, guacd failure handling, layout restore, connection dialog, credential reveal, RDM import wizard, audit, dark mode |

## Manual acceptance checklist (needs real endpoints)

These criteria cannot be automated without real Windows/VNC servers and an Authentik instance.
Run them once per release in a staging deployment.

- [ ] `docker compose up -d` against the external PostgreSQL; all containers healthy; `/readyz` shows database and guacd `true`
- [ ] Authentik login/logout; group → role mapping; user without mapped group is refused
- [ ] RDP to Windows Server with NLA: login, clipboard both directions, Ctrl+Alt+Del, drive upload/download, resizing the browser/split pane changes the remote resolution, high-DPI screen gets the correct scale factor
- [ ] RDP with self-signed certificate: refused with `validate`, works with `tofu`/`ignore` (warning badge shown); `RDP_CERT_ALLOW_INSECURE=false` blocks it
- [ ] VNC (TigerVNC/x11vnc): password auth, scaling, "encryption not guaranteed" badge; same via `ssh_tunnel` (badge disappears)
- [ ] SSH: password, key + passphrase, jump host, changed host key is refused
- [ ] SFTP child of an SSH host inherits hostname and credentials; FTPS explicit and implicit against vsftpd/FileZilla Server with TLS session reuse
- [ ] Several sessions (incl. two to the same host with different accounts) in tabs, floating windows and splits; closing one does not affect the others
- [ ] Network interruption → automatic reconnect; API restart → sessions reconnect
- [ ] Infisical and OpenBao: test, browse, map, sync, conflict, resolve
- [ ] Import a real RDM `.rdm` export; review the unsupported-settings report
- [ ] Backup and restore per [backup-restore.md](backup-restore.md)
