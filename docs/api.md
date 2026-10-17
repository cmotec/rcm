# REST API (v1)

The complete, generated specification is in [openapi.json](openapi.json)
(regenerate with `python -m app.cli openapi docs/openapi.json`). A live instance serves it at
`/api/openapi.json`, with interactive docs at `/api/docs` (`API_DOCS_ENABLED`).

## Conventions

| Topic | Rule |
|---|---|
| Base path | `/api/v1` |
| Auth (browser) | session cookie + `X-CSRF-Token` header on POST/PUT/PATCH/DELETE (token from `GET /auth/me` or the `csrf` cookie) |
| Auth (scripts) | `Authorization: Bearer pat_…` personal access token (*API tokens* in the UI). Read-only tokens reject unsafe methods and never reveal secrets. |
| Format | JSON in and out. Uploads: raw `application/octet-stream` body. RDM import: `multipart/form-data`. |
| Errors | RFC 9457 `application/problem+json`: `{type, title, status, code, detail, request_id, …}`. Validation errors include `errors[]`. |
| Pagination | `?limit=` (1–1000, default 100) and `?offset=`. Responses: `{items, total, limit, offset}`. |
| Filtering | resource-specific query parameters (e.g. `q`, `protocol`, `folder_id`, `tag`, `favorite`) |
| Concurrency | editable resources have `version`. Send it with PATCH, and a mismatch returns `412 version_mismatch`. |
| Idempotency | `Idempotency-Key: <unique>` on resource-creating POSTs. A retry with the same key and body replays the original response (`Idempotent-Replayed: true`). A different body returns 409. |
| Secrets | never returned by default: responses carry `password_set`, `private_key_set`, … Only `POST /credentials/{id}/reveal` returns values (audited). |
| Rate limits | per user (`RATE_LIMIT_API_PER_MINUTE`) and per login source. `429` responses include `Retry-After`. |
| Audit | every change, login, reveal, session event and file operation is written to the audit log |

## Endpoints

### Auth & current user
| Method | Path | Description |
|---|---|---|
| GET | `/auth/config` | public: display name, enabled login methods |
| GET | `/auth/oidc/login?return_to=` | start OIDC login (redirect) |
| GET | `/auth/oidc/callback` | OIDC redirect URI |
| POST | `/auth/local/login` | local recovery login |
| POST | `/auth/logout` | end the browser session; returns the IdP logout URL |
| GET | `/auth/me` | current user, roles, permissions, CSRF token |
| GET/PUT | `/me/preferences` | user preferences (incl. saved workspace layout) |
| GET/POST/DELETE | `/me/tokens[/{id}]` | personal API tokens |
| GET | `/me/accounts` | my personal accounts (`user`, `privileged`): username, domain, `has_password` (passwords are never returned) |
| PUT/DELETE | `/me/accounts/{user\|privileged}` | set (`username`, `domain`, `password` – omitted = unchanged, `clear_password`) or remove one |
| POST | `/me/logout-everywhere` | end all browser and remote sessions |

### Tree, folders, connections
| Method | Path | Description |
|---|---|---|
| GET | `/tree` | all folders and connections visible to the caller (flat lists) |
| GET/POST | `/folders` | list / create |
| GET/PATCH/DELETE | `/folders/{id}` | get / update (rename, move via `parent_id`, icon, colour, search domain) / delete (`?recursive=true`) |
| GET | `/connections` | list & search (`q`, `protocol`, `folder_id`, `parent_id`, `tag`, `favorite`, paging) |
| POST | `/connections` | create (`folder_id` or `parent_id`, protocol settings, auth mode, …) |
| GET/PATCH/DELETE | `/connections/{id}` | get / update / delete (`?recursive=true` for children) |
| GET | `/connections/{id}/effective` | effective hostname/port/search domain/credentials, each with its **source** and whether it is inherited |
| POST | `/connections/{id}/move` | move to a folder, under a parent, or to the root |
| POST | `/connections/{id}/duplicate` | duplicate (optionally with children) |
| PUT/DELETE | `/connections/{id}/favorite` | favorites |

### Credentials & external secrets
| Method | Path | Description |
|---|---|---|
| GET/POST | `/credentials` | list (metadata only) / create (all fields optional) |
| GET/PATCH/DELETE | `/credentials/{id}` | metadata / update (write-only secrets; `null` clears) / delete |
| POST | `/credentials/bulk/move` | `{ids, folder_id}` (`null` = no folder): move several credentials (each needs edit, the target folder edit) → `{done, failed: [{id, name, error}]}` |
| POST | `/credentials/bulk/delete` | `{ids}`: delete several (each needs manage; credentials still in use are kept and listed in `failed`; a parent deleted with its children is fine) → `{done, failed}` |
| POST | `/credentials/{id}/reveal` | reveal selected secret fields (needs `credentials.reveal` + manage; audited) |
| GET | `/credentials/{id}/usage` | referencing connections and child credentials |
| GET/PUT | `/credentials/{id}/mappings` | external secret mappings per field (`attr:<name>` fills an attribute, `secret:<name>` a secret attribute) |
| GET/POST | `/credential-folders` | credential folders visible to the caller / create |
| PATCH/DELETE | `/credential-folders/{id}` | rename, move / delete (`move_contents=true` moves credentials and sub-folders up; nothing is deleted with the folder) |
| POST | `/secret-mappings/{id}/resolve` | resolve a sync conflict (`accept_remote` / `keep_local`) |
| GET/POST | `/secret-providers` | providers (OpenBao) |
| PATCH/DELETE | `/secret-providers/{id}` | update / remove |
| POST | `/secret-providers/{id}/test` | connectivity & auth check |
| GET | `/secret-providers/{id}/browse?path=` | list secret names (never values) |
| POST | `/secret-providers/{id}/sync[?credential_id=]` | synchronise now |
| GET | `/secret-providers/{id}/runs` | sync history |
| POST | `/secret-providers/{id}/import` | create credential entries from external secrets |

### Sessions
| Method | Path | Description |
|---|---|---|
| POST | `/sessions` | start a session. `428 credentials_required` → resend with `credentials`. `428 credential_choice_required` → resend with `credential_id` (a credential the user may use). `auth_override` (`{mode: "own", credentials}`, `{mode: "credential", credential_id}` or `{mode: "account", account: "user"|"privileged"}` – one of the caller's personal accounts) replaces the configured login for this session only ("Open with different credentials"). `409 hostkey_unknown/hostkey_changed` (body has `host_key`) → resend with `accept_host_keys` |
| GET | `/sessions[?scope=all]` | my sessions (or all, with `sessions.manage`) |
| GET | `/sessions/{id}` | status |
| POST | `/sessions/{id}/reconnect` | new one-time ticket (RDP/VNC/SSH) or reopen (file sessions) |
| DELETE | `/sessions/{id}` | disconnect (owner, or admin with `sessions.manage`) |
| POST | `/sessions/disconnect-all` | close all my sessions |
| WS | `/sessions/{id}/tunnel?ticket=…&width=&height=&dpi=&tz=` | Guacamole tunnel (subprotocol `guacamole`) |
| WS | `/sessions/{id}/terminal?ticket=…&cols=&rows=` | native SSH terminal (binary data + JSON control) |
| GET | `/sessions/{id}/fs/home` · `/fs/list?path=` · `/fs/stat?path=` | file manager (SFTP/FTP/FTPS) |
| POST | `/sessions/{id}/fs/mkdir` · `/fs/rename` · `/fs/delete` · `/fs/chmod` | file operations |
| GET | `/sessions/{id}/fs/download?path=` | streamed download |
| PUT | `/sessions/{id}/fs/upload?path=&overwrite=` | streamed upload (raw body) |
| GET | `/sessions/{id}/transfers` | transfer progress / errors |

### Administration
| Method | Path | Description |
|---|---|---|
| GET/POST, GET/PATCH/DELETE | `/users`, `/users/{id}` | users (`users.manage`) |
| GET | `/users/lookup?q=` | minimal directory for ACL editing |
| GET/POST, PATCH/DELETE | `/roles`, `/roles/{id}` | roles (custom roles editable) |
| GET | `/permissions` | all global permission names |
| GET | `/groups` | IdP groups seen at login |
| GET/POST/DELETE | `/group-mappings[/{id}]`, GET `/group-mappings/env` | IdP group → role mappings |
| GET/PUT | `/acl/{folder|connection|credential|cred_folder}/{id}` | resource grants (all-zero UUID = root) |
| GET | `/acl/effective?resource_type=&resource_id=&user_id=` | effective permission |
| GET/POST/DELETE | `/known-hosts[/{id}]` | trusted SSH host keys |
| GET | `/audit` | audit log (`action` supports `prefix.*`, `actor_id`, `resource_*`, `outcome`, `since`, `until`) |
| GET/PATCH | `/settings` | global runtime settings (cert/host-key defaults, banner) |
| POST | `/imports/rdm` | upload RDM export → preview |
| GET | `/imports`, `/imports/{id}` | history / preview / report |
| POST | `/imports/{id}/apply` | selective import |
| GET | `/system/info` | version, feature flags |
| GET | `/healthz`, `/readyz` | probes (also under `/api/`) |

## Example (curl)

```bash
T=pat_xxxxxxxx   # created under "API tokens"
API=https://connections.example.com/api/v1
curl -sH "Authorization: Bearer $T" "$API/connections?q=web&protocol=ssh"
curl -sH "Authorization: Bearer $T" -H 'Content-Type: application/json' -H 'Idempotency-Key: 7f9c…' \
  -d '{"name":"web01 SSH","protocol":"ssh","parent_id":"<host-id>","port":22}' "$API/connections"
curl -sH "Authorization: Bearer $T" "$API/connections/<id>/effective"
```
