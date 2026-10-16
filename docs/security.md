# Security

## 1. Assets

| Asset | Where | Protection |
|---|---|---|
| Remote credentials (passwords, SSH keys, passphrases) | `credentials.*_enc`, `connections.password_enc` | AES-256-GCM, key outside the DB, AAD binds each ciphertext to its row and column |
| External-secret provider credentials | `secret_providers.secret_config_enc` | same |
| OIDC refresh/ID tokens | `web_sessions.*_enc` | same |
| Session cookies, API tokens | browser / client | only SHA-256 hashes are stored server-side |
| Interactive credentials | API process memory, per session | never persisted, wiped when the session closes |
| Live remote sessions | API ↔ guacd ↔ target | owner-bound one-time tickets, guacd isolated |
| Audit trail | `audit_events` | append-only by the application, secrets redacted |

## 2. Trust boundaries

```
Internet/LAN ──TLS──► web (nginx) ──► api ─┬─► PostgreSQL (TLS, verify-full recommended)
                                            ├─► guacd (private network, no published port) ──► RDP/VNC targets
                                            ├─► SSH / SFTP / FTP(S) targets
                                            └─► IdP, OpenBao (TLS)
```

The browser is untrusted. The api is the only component that knows remote credentials and
the only one allowed to talk to guacd.

## 3. Controls

**Authentication.** OIDC authorization code flow with PKCE (S256), `state` in a signed,
short-lived, path-scoped cookie, `nonce` check, JWKS signature validation (asymmetric algorithms only),
issuer/audience/`azp`/expiry checks, and periodic refresh-token re-validation. Local accounts
(Argon2id) are for recovery and are disabled by default. Login is rate-limited per IP and per
username.

**Sessions.** Random 256-bit cookie (`__Host-session`, HttpOnly, Secure, SameSite=Lax), idle and
absolute expiry, server-side revocation (logout, "log out everywhere", user deactivation).

**CSRF.** Every unsafe request authenticated by cookie must carry `X-CSRF-Token`, which matches
the per-session token stored server-side. A foreign `Origin` header is rejected as well. API tokens
(bearer) are not ambient credentials and skip the CSRF check.

**WebSockets.** `Origin` must equal `APP_URL` (prevents cross-site WebSocket hijacking). Each
attach needs a one-time ticket (30 s, bound to user + session). Only one attachment per session is
allowed. Permission is re-checked on attach and on every file operation.

**Authorisation.** Global role permissions plus resource ACLs (view/connect/edit/manage) that
inherit down folders and parent connections, capped per role. Revealing a secret needs
`credentials.reveal` plus *manage* on the credential, and is audited with an optional reason.
Using a credential or jump host in a connection requires *connect* on it. Opening a connection does
not expose the credential to the user.

**Signage accounts.** An administrator can mark an account as a signage display and assign one
RDP or VNC connection (Administration → Users). The server then ignores the account's roles and
ACLs. It has no global permissions (no API tokens, no administration), *connect* on exactly the
assigned connection, and nothing else, so it cannot choose or override credentials. The connection
therefore needs stored credentials. The UI shows only that connection, fullscreen, starts it after
login, and opens a new session whenever it ends. Its login lasts `SIGNAGE_SESSION_MAX_DAYS`
(default 30) without idle timeout, because the display never calls the API while it only shows a
screen. Deactivate the account (or change its password) to end the display's login. Deleting the
assigned connection leaves the account a signage account with nothing to show.

**guacd hardening.** guacd is unreachable from clients. The api performs the handshake and
forwards only an allow-list of client opcodes (`key`, `mouse`, `size`, `clipboard`, …). Any
other opcode (`select`, `connect`, …) closes the tunnel. Uploads to the RDP drive are dropped when the
connection disables them. Each user gets their own drive directory.

**Transport warnings.** FTP, VNC without SSH tunnel, disabled RDP certificate validation,
FTPS without verification and automatic SSH host-key acceptance are flagged in the tree, the
details panel and the session header. RDP defaults to certificate validation. Insecure
policies can be disabled globally (`RDP_CERT_ALLOW_INSECURE=false`).

**SSH host keys.** Keys are verified before the session exists. Unknown keys need explicit user
confirmation (`prompt`). Changed keys are refused unless a user with edit rights (or
`known_hosts.manage`) replaces them. Strict mode only accepts keys pinned by an administrator.

**Input handling.** Pydantic validation with `extra="forbid"` on every body, protocol settings
validated per protocol, path normalisation for file operations, and `defusedxml` for imports
(entities/DTDs refused).

**Containers.** Non-root users, `cap_drop: ALL`, `no-new-privileges`, read-only root
filesystems for web and api, private networks, and no published guacd port.

**Logging.** A structlog processor redacts sensitive keys (`password`, `token`, `secret`,
`private_key`, `ticket`, …) and patterns (PEM private keys, bearer tokens, `ticket=` query
parameters). uvicorn's access log is disabled, because the app logs paths without query strings.

**Browser.** Strict CSP (`script-src 'self'`, `frame-ancestors 'none'`, no inline scripts),
`X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, HSTS with TLS, and API responses marked
`no-store`.

## 4. Threat model (STRIDE summary)

| Threat | Example | Mitigation | Residual risk |
|---|---|---|---|
| Spoofing | stolen session cookie | HttpOnly/Secure cookie, idle timeout, IdP re-validation, revocation | XSS would still allow actions in the victim's browser; CSP reduces it |
| Spoofing | attacker IdP token | signature, issuer, audience, nonce checks | compromised IdP = compromised app |
| Tampering | ciphertext swapped between rows | AES-GCM AAD (table.column.row_id) | – |
| Tampering | MITM to SSH target | host-key verification with TOFU prompt / strict mode | users accepting unknown keys without checking |
| Tampering | MITM to RDP target | certificate validation (default) | operators choosing `ignore`/`tofu` |
| Repudiation | user denies opening a session or revealing a password | audit log for logins, session lifecycle, reveals, file operations, ACL and settings changes | DB administrators can alter the audit table; forward logs to a SIEM for tamper evidence |
| Information disclosure | credentials in API responses or logs | write-only secret fields, `*_set` flags, redaction, explicit reveal endpoint | – |
| Information disclosure | DB dump leaked | secrets encrypted with key outside DB | key + dump together = disclosure; keep keys in Docker secrets/KMS |
| Information disclosure | another user attaches to my RDP session | owner-bound tickets, NotFound for foreign sessions, single attachment | administrators can terminate (not view) sessions |
| Denial of service | many sessions / logins | per-user session limit, rate limits, timeouts | single API process is a capacity limit |
| Elevation of privilege | browser drives guacd directly | guacd not reachable; opcode allow-list | guacd/FreeRDP vulnerabilities: keep guacd updated |
| Elevation of privilege | read-only user edits | role resource cap, read-only API tokens | – |
| Elevation of privilege | malicious import file (XXE, billion laughs) | defusedxml, size limit, nothing executed | – |

## 5. Operator checklist

- [ ] TLS everywhere: `APP_URL=https://…`, `COOKIE_SECURE=true`, `DB_SSLMODE=verify-full`
- [ ] Encryption keys in Docker secrets or a secret manager, with a backup kept separately from DB backups
- [ ] `LOCAL_AUTH_ENABLED=false` in normal operation
- [ ] `OIDC_DEFAULT_ROLE=none`, roles granted through groups
- [ ] `RDP_CERT_ALLOW_INSECURE=false` if all RDP hosts have valid certificates
- [ ] Forward container logs (including `audit` events) to central logging
- [ ] Keep guacd and base images updated (`docker compose pull && docker compose up -d`)
- [ ] Regularly review *Administration → Audit log* for `denied` and `failure` outcomes
