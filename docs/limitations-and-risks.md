# Limitations and risks

This list was written before implementation and is kept up to date. Each item
says what the limitation is, why it exists, and what to do about it.

## Architectural

| # | Limitation / risk | Impact | Mitigation / plan |
|---|---|---|---|
| A1 | **Live session state is held in the memory of one API process.** Tunnels, SSH channels and FTP control connections are sockets owned by one process. | The `api` service must run as **one replica with one uvicorn worker**. A restart drops every live session (clients reconnect on their own, and RDP returns to the same Windows session). | Vertical scaling is enough for hundreds of concurrent sessions, since the workload is I/O-bound. Plan for horizontal scale: sticky routing by session ID at the proxy, plus a session-registry table to route reconnects to the owning node. Not implemented. |
| A2 | Rate limiting and one-time tickets are kept in memory. | Same as A1: correct only with one API process. | Move them to PostgreSQL (`UNLOGGED` table) or Redis together with A1. |
| A3 | guacd connections cannot be resumed. A dropped WebSocket ends the remote session on the guacd side. | "Reconnect" is a new RDP/VNC connection. Windows reattaches the user session, but VNC/SSH-over-guacd lose their state. | Native SSH engine (default) also reconnects with a new shell. Keeping SSH channels alive while detached is possible later (buffer output for the grace period). |
| A4 | Only one tunnel may attach to a session at a time. | Opening the same session in two browser tabs is refused. Opening the same *connection* twice works and creates two sessions. | Deliberate: prevents session hijacking. |

## Protocol-specific

| # | Limitation / risk | Mitigation / plan |
|---|---|---|
| P1 | VNC encryption: guacd does not expose VeNCrypt certificate validation. | VNC is marked "encryption not guaranteed" unless SSH-tunnelled. Use `settings.ssh_tunnel`. |
| P2 | RDP certificate validation runs inside guacd/FreeRDP, which has its own trust store. | Mount your CA into the guacd container ([deployment.md](deployment.md)). `cert-tofu` / `cert-fingerprints` need guacd ≥ 1.5. Older guacd ignores them, with a warning in the API log. |
| P3 | RDP drive redirection stores uploaded files in the guacd container (`/drives/<user-id>`, a named volume). | Files are per user, not per connection. The volume should be cleaned up by policy. `RDP_DRIVE_ENABLED=false` turns the feature off. |
| P4 | VNC server-side resizing depends on guacd version and server (ExtendedDesktopSize). | The client always scales to fit. A `size` request is sent anyway and ignored if unsupported. |
| P5 | FTP via `ftplib` is blocking and runs in one worker thread per FTP session. Operations within one FTP session are serialised. | Users who want parallel transfers open a second session to the same host. Thread count is capped by `FTP_MAX_SESSIONS`. |
| P6 | FTP active mode needs the FTP server to connect back to the API container. In Docker this usually needs `network_mode: host` or published port ranges. | Passive mode is the default. Active mode is supported and documented, but needs network setup. |
| P7 | Implicit FTPS uses a small `ftplib.FTP_TLS` subclass (wraps the socket before the greeting). Some servers require TLS session reuse on the data channel. That is implemented too, but some server/TLS-library combinations may still fail. | Test against your servers. Error messages are shown in the UI. |
| P8 | SSH via guacd (`engine=guacd`) has no jump hosts and no interactive host-key check. Only a pinned `host-key` string is supported. | Native SSH engine is the default. |

## Import from Devolutions Remote Desktop Manager

| # | Limitation |
|---|---|
| I1 | The `.rdm` export is XML, and its schema is not formally published. The importer is tolerant: it reads the documented, commonly seen elements (`Connection`, `ConnectionType`, `Name`, `Group`, `Url`/`Host`, `Port`, `ID`, `ParentID`, `CredentialConnectionID`, `Description`, `Keywords`/`Tags`, `RDP`/`Terminal`/`VNC`/`FTP` sub-elements). Everything it does not understand goes into the **unsupported-settings report**. Nothing is dropped silently. |
| I2 | Passwords in RDM exports are encrypted (`SafePassword` / `ProtectedString`), with the data source key or the export master key. **The importer never tries to decrypt them.** Such fields are reported as "credential not migrated". The report links to the manual workflow: create a credential entry or map an external secret, then assign it. |
| I3 | CSV export from RDM: columns are mapped by header name. Columns that are not mapped are listed. |
| I4 | Unsupported entry types (web, PowerShell, VPN, documents, …) are listed and skipped. They are never turned into something else. |

## Identity and secrets

| # | Limitation / risk | Mitigation |
|---|---|---|
| S1 | The credential-encryption key is in the API's environment. Anyone with root on the Docker host can read it. | Recommended: Docker secrets (`ENCRYPTION_KEYS_FILE`). Key rotation is supported (`python -m app.cli rotate-keys`). Using an HSM/KMS is future work. |
| S2 | Infisical and OpenBao APIs change between versions. | Providers use stable documented endpoints (Infisical universal-auth + `/api/v3/secrets/raw`, OpenBao/Vault KV v2 + token/AppRole auth). Each provider has a "test connection" endpoint. |
| S3 | Write-back to external secret stores is not implemented. Sync is one-way, external → local. | Local edits to synced fields count as a conflict (see [secret-providers.md](secret-providers.md)). Write-back can be added per provider through the provider interface. |
| S4 | OIDC back-channel logout is not implemented. Front-channel/RP-initiated logout is. | Sessions also expire through `AUTH_SESSION_IDLE_MINUTES` / `AUTH_SESSION_MAX_HOURS`. |

## Verification limits of this build environment

The repository was built on a Windows machine without Docker. What was verified:

* 69 backend tests (pytest) against a real PostgreSQL 16 (temporary instance), including the
  migration. They cover the guacd handshake/relay against a fake guacd speaking the real wire
  protocol, native SSH and SFTP against an asyncssh server, FTP against pyftpdlib, OIDC against a
  mocked IdP with real signatures, and secret sync.
* The frontend: strict TypeScript build and layout-model unit tests. It was also driven end to end
  in headless Chromium (Playwright) against the real API: live terminal, SFTP/FTP, tabs,
  floating windows, split view, restore, dialogs and import (see `e2e/`).

**Not verified here:** the Docker images and Compose files were not built or run. There was
no live guacd, so actual RDP/VNC rendering was not seen. Real Windows/VNC servers, FTPS against a
TLS server, Authentik and Infisical/OpenBao instances were not available. Please run the
checklist in [testing.md](testing.md) in a staging deployment before production.

## Dependencies

| # | Note |
|---|---|
| D1 | `guacamole-common-js` comes from npm as `guacamole-common-js@1.5.0`. This is a community re-packaging (ESM/CJS) of the official Apache Guacamole 1.5.0 JavaScript sources, and Apache does not publish to npm. It is wrapped in `frontend/src/sessions/guac.ts`. To use Apache's own build instead, vendor `guacamole-common-js` from the official release and keep the wrapper. Protocol 1.5 is compatible with guacd 1.6. |
