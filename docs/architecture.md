# Architecture

> Product name: **Remote Connections Manager** (constant `APP_NAME` in `backend/app/config.py` and
> `frontend/src/appName.ts`; container images `remote-connections-manager-{api,web,guacd}`).

## 1. Goals and shape of the system

A self-hosted manager for remote connections (SSH, SFTP, RDP, VNC, FTP, FTPS).
Users sign in through OIDC (Authentik first), browse a tree of folders and
connections, and open many sessions at once. The sessions show up as tabs,
floating windows or split views in one browser workspace.

```
               Browser (custom TypeScript SPA)
   ┌───────────────────────────────────────────────────────────┐
   │ connection tree │ workspace: tabs / floating / split view │
   │                 │  ├ RDP/VNC  → Guacamole display (canvas) │
   │                 │  ├ SSH      → xterm.js                   │
   │                 │  └ SFTP/FTP → file manager (REST)        │
   └──────────┬──────────────────────────┬────────────────────┘
        HTTPS │ REST /api/v1         WSS │ /api/v1/sessions/{id}/…
   ┌──────────▼──────────────────────────▼────────────────────┐
   │ web (nginx, unprivileged): static SPA + reverse proxy    │
   └──────────┬───────────────────────────────────────────────┘
              │ (private network "app")
   ┌──────────▼───────────────────────────────────────────────┐
   │ api (Python 3.12, FastAPI, uvicorn)                      │
   │  auth (OIDC, local recovery admin, API tokens)           │
   │  RBAC + ACLs, folders, connections, credential store     │
   │  inheritance + DNS search-domain resolution              │
   │  session manager:                                        │
   │    guac tunnel  ─────────────── TCP 4822 ──┐             │
   │    SSH terminal  (asyncssh)    ───────────────────────►  SSH hosts
   │    SFTP          (asyncssh)    ───────────────────────►  SFTP hosts
   │    FTP/FTPS      (ftplib, threads) ───────────────────►  FTP hosts
   │  secret sync (OpenBao / Vault) ────────────────────────►  secret stores
   │  RDM import, audit log, background jobs                  │
   └──────────┬─────────────────────────────┬─────────────────┘
              │ asyncpg (TLS optional)      │ (private network "gateway")
   ┌──────────▼──────────┐       ┌──────────▼──────────┐
   │ external PostgreSQL │       │ guacd (no published │──► RDP / VNC hosts
   │ (not in compose)    │       │ port)               │    (SSH optional)
   └─────────────────────┘       └─────────────────────┘
```

## 2. Frontend framework: React + TypeScript + Vite

Candidates: React 19, Vue 3, Svelte 5, SolidJS.

What the frontend has to do well:

1. **Keep long-lived, stateful, imperative widgets alive while the layout around
   them changes.** A Guacamole display, an xterm.js terminal and an open file
   browser must survive moving from a tab into a floating window or a split
   pane. No framework reparents components without remounting them. So the
   design does not rely on the framework for this: every session owns a DOM
   element created by a plain TypeScript `SessionManager`, and layout
   components only *borrow* that element and append it to their slot. When a
   slot unmounts, the element moves to a hidden parking container. Canvas
   bitmaps and terminal buffers survive DOM moves. This means the choice of
   framework hardly affects the hardest requirement.
2. **Complex interactive chrome**: tree with drag and drop, context menus,
   dialogs, forms with "inherited vs. explicit" indicators, a window manager
   with drag, resize, z-order and split dividers.
3. **Real-time updates** for session state and transfer progress. These are
   low-frequency UI updates. Pixel data never goes through the framework
   because guacamole-common-js and xterm.js draw directly.
4. **Long-term maintainability**: ecosystem, typing quality, hiring, stability.

| Criterion | React | Vue | Svelte 5 | Solid |
|---|---|---|---|---|
| Fine-grained updates for window chrome | good (with selector stores) | very good | very good | excellent |
| Ecosystem (forms, DnD, query caching, a11y) | largest | large | medium | small |
| TypeScript experience | excellent | good | good | good |
| Long-term stability / hiring | best | good | ok | weak |
| Fit for imperative widget hosting | good (refs/effects) | good | good | good |

**Decision: React 19 + TypeScript + Vite**, with **Zustand** for workspace and
session state (selector subscriptions avoid re-rendering every window when one
moves) and **TanStack Query** for REST data. The window manager (tabs, floating
windows, split view) is **custom**. Off-the-shelf docking libraries do not
model "minimise to a taskbar", "first session fills the workspace" or our
DOM-borrowing session lifetime without fighting them. A custom model (below)
is about a thousand lines and fully under our control.

Svelte 5 would also work, and its runtime is smaller. React wins on ecosystem
depth and long-term maintainability. The decisive technical problem (session
survival) is solved outside the framework either way.

### Workspace model

```ts
Workspace = {
  tabs: Tab[]                  // tab strip, left → right
  activeTabId: string | null
  floating: FloatingWindow[]   // overlapping windows above the tab area
}
Tab            = { id, root: LayoutNode }
LayoutNode     = { kind: 'leaf', sessionId }
               | { kind: 'split', dir: 'row' | 'column', children: LayoutNode[], sizes: number[] }
FloatingWindow = { id, sessionId, x, y, w, h, z, minimized, maximized }
```

* The first session opens as the only tab and fills the workspace. Each
  further session opens a new tab, which also fills the workspace.
* Dragging a tab onto the workspace body creates a floating window. Dragging
  it onto an edge drop zone of the visible tab splits that tab.
* Floating windows can be moved, resized, minimised to the taskbar, maximised,
  restored and raised. Dragging a window's title bar onto the tab strip turns
  it back into a tab.
* Split panes have draggable dividers. "Move to tab" pulls a pane out.
* The layout (no secrets, only connection IDs and geometry) is saved to user
  preferences. After a reload it is restored and the sessions are offered for
  reconnection.

## 3. Backend: Python 3.12 + FastAPI

FastAPI was evaluated as the preferred starting point and kept:

* native `async`/`await`. Every remote session is long-lived I/O, so one event
  loop handles hundreds of tunnels.
* first-class WebSockets (Starlette) for the Guacamole tunnel and SSH terminal.
* automatic OpenAPI 3 documentation from Pydantic models.
* Pydantic v2 validation for every request body.

Libraries:

| Concern | Library | Why |
|---|---|---|
| HTTP / WS | FastAPI + uvicorn (uvloop, httptools) | async, OpenAPI |
| ORM / DB | SQLAlchemy 2 (async) + asyncpg | mature, typed, pooling, TLS |
| Migrations | Alembic | standard for SQLAlchemy |
| Config | pydantic-settings | typed env configuration |
| Crypto | `cryptography` (AES-256-GCM) | audited primitives |
| Password hashing (recovery admin) | argon2-cffi | memory-hard |
| OIDC | httpx + PyJWT (JWKS) | small, explicit code-flow + PKCE |
| SSH / SFTP | asyncssh | pure-Python async, jump hosts, key auth |
| FTP / FTPS | stdlib `ftplib` in worker threads | covers active/passive, explicit + implicit TLS, ASCII/binary, encoding; maintained with CPython |
| DNS | dnspython (async resolver) | search-domain probing without touching host DNS |
| XML import | defusedxml | safe parsing of untrusted RDM exports |
| Logging | structlog (JSON) with secret redaction | structured logs |

### Module layout

```
backend/app
├── main.py              app factory, middleware, routers, lifespan
├── config.py            Settings (env)
├── logging.py           structlog + redaction
├── db.py                engine, session factory, health check
├── models.py            SQLAlchemy models
├── schemas/             Pydantic request/response models
├── api/
│   ├── deps.py          auth, current user, permission dependencies
│   ├── errors.py        problem+json error handling
│   └── v1/              routers: auth, users, roles, folders, connections,
│                        credentials, sessions, files, secrets, imports,
│                        audit, settings, tokens, known-hosts
├── security/            crypto (AES-GCM envelope), csrf, rate limiting,
│                        passwords, tokens
├── services/
│   ├── authz.py         RBAC + resource ACL evaluation
│   ├── resolution.py    hostname / port / credential / search-domain inheritance
│   ├── dns.py           search-domain resolution
│   ├── audit.py
│   ├── oidc.py
│   ├── sessions/        manager, guacd protocol client, parameter builders,
│   │                    SSH terminal, SFTP and FTP file systems
│   ├── secrets/         provider interface, OpenBao, automatic import, sync engine
│   └── importers/       Devolutions RDM (.rdm XML, CSV)
├── jobs.py              background scheduler (session reaper, secret sync)
└── cli.py               migrations, recovery admin, key rotation
```

## 4. Protocol responsibilities

| Protocol | Implemented by | Transport to browser | Credentials passed | Authorisation enforced |
|---|---|---|---|---|
| RDP | guacd (FreeRDP) | Guacamole protocol over our WebSocket tunnel | backend resolves the credentials and sends them in the guacd `connect` handshake. They never reach the browser. | backend: `connect` permission when the session is created. One-time ticket and owner check when the tunnel is attached. |
| VNC | guacd (libvncclient) | same as RDP | same as RDP | same as RDP |
| SSH (default) | backend, asyncssh | xterm.js over our WebSocket (binary data, JSON control) | backend authenticates with password / key / passphrase held in memory | same as RDP |
| SSH (optional `engine=guacd`) | guacd | Guacamole display | `connect` handshake | same as RDP |
| SFTP | backend, asyncssh SFTP | REST (list/stat/mkdir/rename/delete), streamed upload/download | in memory in the session object | session owner + connection permission on every request |
| FTP / FTPS | backend, `ftplib` in a dedicated worker thread per session | REST, same API as SFTP | same as SFTP | same as SFTP |

**Why SSH defaults to the backend, not guacd.** guacd can do SSH, and it is
supported as an engine option. But guacd renders the terminal on the server
and streams it as images. It has no jump-host support and only checks host
keys against a static string. With asyncssh + xterm.js we get jump hosts,
interactive trust-on-first-use host-key verification stored in the database,
native copy/paste, accurate resizing and far less bandwidth. This goes
against the spec's suggestion to use guacd for SSH. It is a deliberate
choice, and a connection can be switched to `engine=guacd`.

Interactive sessions (RDP, VNC, SSH) and file-transfer sessions (SFTP, FTP,
FTPS) are separate session kinds with separate endpoints. RDP drive
redirection is a guacd feature. It is exposed in the RDP window, not through
the file-manager API.

## 5. Inheritance and resolution rules

All resolution happens in `services/resolution.py`. The API exposes the
result and its *source* through `GET /connections/{id}/effective`. The UI
uses this to mark values as *inherited* or *explicit*.

### Hostname
1. The connection's own `hostname`, if set.
2. Otherwise the parent connection's effective hostname (recursively).
3. Otherwise the connection is not launchable (validation error at launch).

Changing a parent's hostname changes every child that has no own hostname,
because nothing is copied.

### Port
1. The connection's own `port`.
2. Otherwise the protocol default (SSH/SFTP 22, RDP 3389, VNC 5900 + display
   number, FTP 21, implicit FTPS 990, explicit FTPS 21). Ports do not inherit
   from parents, because they depend on the protocol.

### DNS search domain
Applied only to a *single-label* effective hostname. A hostname is left
untouched if it contains a dot, ends with a dot, or is an IP address.
1. The connection's own `search_domain`.
2. Otherwise the nearest ancestor connection's `search_domain`.
3. Otherwise the nearest folder's `search_domain` (walking up the folders).
4. Otherwise `DNS_SEARCH_DOMAINS` from the environment (comma-separated,
   tried in order).

When several candidates exist, each `host.domain` candidate is resolved with
the backend's DNS resolver and the first one that resolves wins. If none
resolve, the first candidate is used and the error comes from the protocol
layer. The bare name is tried last. The Docker host's DNS configuration is
never changed.

### Credentials (field by field)
Fields: `username`, `password`, `domain`, `private_key`, `passphrase`.
SSH/SFTP with both a private key and a password: the key is tried first, the password is the
fallback (also when the key cannot be loaded, e.g. wrong passphrase). guacd's SSH client has no
fallback of its own, so for the guacd SSH engine the api first checks the key with a key-only login
and hands guacd only the password if the key is rejected.
`auth_mode` decides where a connection's own values come from:

| `auth_mode` | Own values |
|---|---|
| `inherit` (default) | none of its own |
| `direct` | `username`, `password`, `domain` stored on the connection |
| `credential` | the referenced credential entry (including that entry's own parent chain). If no entry is pinned, the user picks one of the credentials they may use (*connect* permission) when opening the connection |
| `interactive` | entered by the user at launch, kept in memory only for the session's lifetime |

For each field the first non-empty value wins, in this order:
1. Interactive input (only for `interactive`).
2. The connection's own values for its `auth_mode`.
3. If `inherit_auth` is true (the default): the parent connection's effective
   value, using the same algorithm recursively.

If `inherit_auth` is false the connection is fully independent. Credential
entries have the same parent rule (`parent_id`): an empty field falls back to
the parent credential. Cycles are rejected on write. During resolution the
depth is capped at 32.

### Lookups (values from the credential store)

Any of `username`, `password`, `domain`, `private_key`, `passphrase`, `hostname`, `port`,
`search_domain` and `description` can hold a lookup instead of a value:

```
{{lookup:<credential name pattern>:<field>}}      e.g. {{lookup:{hostname}_root:username}}
```

* Placeholders: `{hostname}` (host name of the **opened** connection, so a lookup on a parent applies to
  each child with its own host), `{name}` (its name), `{domain}` (its search domain). A hostname
  lookup cannot use `{hostname}`, a domain lookup cannot use `{domain}`.
* `*` matches any text; names are compared case-insensitively and must match completely.
* Only credentials the user may use count (`connect` when opening, `view` for the details shown).
  Exactly one must match; no match or several matches is an error, nothing is guessed.
* `<field>`: a login field of the credential, one of its **attributes** (key/value pairs shown to
  everyone who may view the credential, e.g. `port`, `description`), or one of its **secret
  attributes** (write-only like the password, e.g. an API token). The OpenBao import fills both from
  the secret's other keys. Secrets (password, key, passphrase, secret attributes) are decrypted only
  for the session; the tree and the details panel never show them.
* Attributes and secret attributes are stored encrypted (AES-256-GCM, bound to credential and column,
  `attributes_enc`, `secret_attributes_enc`).
* A lookup replaces the connection's own value for that field and takes part in inheritance like
  it. Login lookups belong to "connection-specific" authentication.
* A failing lookup is shown in the details panel and the connection dialog; opening the connection
  fails with the message (`lookup_failed`). The tree shows resolved display values.

Stored in `connections.lookups` (field → expression); the plain column of that field stays empty.

### Credential folders

The credential store has its own folder tree (`credential_folders`), separate from connection
folders. Grants on a folder (ACL resource type `cred_folder`) apply to the folders and credentials
below it, like connection folders; the credential-store root grant (`credential`, all-zero UUID)
covers everything. "Choose a credential at connect time" can be limited to one folder (and its
sub-folders): `connections.credential_folder_id`.

### Where interactive credentials live
The values from the interactive prompt are kept on the in-memory session
object, so "reconnect" works without asking again. They are discarded when
the session ends. They are never written to the database or logs.

## 6. Session lifecycle

```
POST /api/v1/sessions {connection_id, credentials?, options?}
   → authorise (connect on connection) → resolve effective config
   → create in-memory Session (state=pending, owner=user)
   → audit "session.create" → 201 {id, kind, ticket, …}
WS   /api/v1/sessions/{id}/tunnel?ticket=…      (guac)   or
WS   /api/v1/sessions/{id}/terminal?ticket=…    (ssh)
   → check cookie/bearer auth + Origin + one-time ticket bound to user and session
   → open upstream (guacd handshake / asyncssh) → state=connected
   ← relay until either side closes → state=disconnected
POST /api/v1/sessions/{id}/reconnect  → new ticket, same resolved config
DELETE /api/v1/sessions/{id}           → close upstream, discard secrets, state=closed
```

A background reaper closes sessions that have been `disconnected` longer than
`SESSION_RECONNECT_GRACE_SECONDS` or idle longer than
`SESSION_IDLE_TIMEOUT_SECONDS`. File-transfer sessions have the same states.
Their "attachment" is the HTTP requests made against them.

Session state lives in the memory of the single API process. See
[limitations-and-risks.md](limitations-and-risks.md) for what this means for
horizontal scaling.

## 7. Further reading

* [guacd-integration.md](guacd-integration.md): handshake, tunnel, security boundaries
* [database-schema.md](database-schema.md)
* [api.md](api.md)
* [deployment.md](deployment.md): Docker Compose, upgrades
* [security.md](security.md): threat model
* [limitations-and-risks.md](limitations-and-risks.md)
