# Deployment

## 1. What gets deployed

| Container | Image | Purpose | Exposed |
|---|---|---|---|
| `web` | built from `frontend/` (nginx-unprivileged) | serves the SPA, reverse-proxies `/api` (incl. WebSockets) | published port (`HTTP_PORT`, default 8080) |
| `api` | built from `backend/` (python:3.12-slim, uid 10001) | REST API, WebSocket tunnels, SSH/SFTP/FTP, secret sync | internal only |
| `guacd` | `guacamole/guacd:1.6.0` | RDP/VNC (and optional SSH) protocol gateway | **never published** |
| `volume-init` | `busybox` (one-shot) | makes the drive/recording volumes writable for guacd | – |

Networks: `app` (web ↔ api) and `gateway` (api ↔ guacd). guacd is not on the `app`
network and has no published port. The api and guacd containers need outbound access to
the remote hosts, and the api container also needs outbound access to PostgreSQL, the IdP and the
secret managers.

PostgreSQL is **external** (PostgreSQL 14 or newer). It is not part of the core
deployment. `docker-compose.postgres.yml` exists for evaluation only.

## 2. Prerequisites

* Docker Engine 24+ with Compose v2.24+ (the TLS overlay uses `!override`).
* An empty PostgreSQL database and a role that owns it:
  ```sql
  CREATE ROLE connections LOGIN PASSWORD '…';
  CREATE DATABASE connections OWNER connections;
  ```
* An OIDC client in Authentik (see §4) or a local recovery admin.
* A DNS name and TLS certificate (either your own reverse proxy, or the TLS overlay).

### Docker management UIs (Arcane, Portainer, …)

`web` and `api` are built from this repository and are not published to a registry. Their image
names (`local/connections-*`) are only local tags. Both services set `pull_policy: build`, so
Compose-compatible tools build them instead of trying a registry pull. Arcane, for example,
otherwise applies its *Default Deploy Pull Policy* (`missing`), which pulls and fails. The tool
needs the whole repository as build context, so use a Git-synced project rather than pasting only
the compose file. `.env` is optional: set the variables from `.env.example` in the tool's
environment editor instead.

## 3. First start

```bash
cp .env.example .env
# generate the encryption key (store a copy in your password manager / KMS!)
echo "ENCRYPTION_KEYS=k1:$(openssl rand -base64 32)"
#   → put that line into .env together with ENCRYPTION_ACTIVE_KEY_ID=k1
$EDITOR .env          # APP_URL, DB_*, OIDC_*, …
docker compose run --rm --no-deps api python -m app.cli check-config
docker compose up -d --build
docker compose ps     # all services healthy
```

The API applies database migrations at start-up (`DB_MIGRATE_ON_START=true`). The migration
takes a PostgreSQL advisory lock, so concurrent starts are safe. To migrate as a separate step,
set it to `false` and run `docker compose run --rm api python -m app.cli migrate`.

### First administrator

* **OIDC:** put yourself in a group listed in `OIDC_ADMIN_GROUPS`, or
  after your first login run
  `docker compose exec api python -m app.cli grant-role <username> admin`.
* **Local recovery account:** set `LOCAL_AUTH_ENABLED=true` with `LOCAL_ADMIN_USERNAME` and
  `LOCAL_ADMIN_PASSWORD` (created once at start-up), or run
  `docker compose exec api python -m app.cli create-admin <name>`.

## 4. Authentik

1. *Applications → Providers → Create → OAuth2/OpenID Provider*
   * Client type: **Confidential**
   * Redirect URI: `${APP_URL}/api/v1/auth/oidc/callback` (strict)
   * Signing key: any RSA/EC certificate (ID tokens must be signed, `RS256`/`ES256`)
   * Scopes: `openid`, `email`, `profile`. Authentik's default *profile* mapping includes
     the `groups` claim.
   * Optional: enable *offline_access* to get refresh tokens. The application then re-validates
     every `OIDC_REVALIDATE_MINUTES` that Authentik still accepts the user.
2. *Applications → Create*, then link the provider. The slug determines the issuer:
   `OIDC_ISSUER=https://<authentik>/application/o/<slug>/` (with trailing slash).
3. Create groups (for example `connections-admins`, `connections-users`, `connections-readonly`)
   and set `OIDC_*_GROUPS` accordingly. More mappings can be added in the UI.
4. *Post-logout redirect*: add `${APP_URL}/` (or `OIDC_POST_LOGOUT_REDIRECT_URL`) to the
   provider's allowed redirect URIs.

Users without any mapped role are refused (`OIDC_DEFAULT_ROLE=none`).

## 5. TLS

Option A, an existing reverse proxy (Traefik, Caddy, HAProxy, …): terminate TLS there,
forward to `web:8080`, keep the `Host` header, allow WebSocket upgrades, and use long read
timeouts (≥ 1 h).

Option B, the TLS overlay:

```bash
mkdir -p certs && cp fullchain.pem certs/tls.crt && cp privkey.pem certs/tls.key
docker compose -f docker-compose.yml -f docker-compose.tls.yml up -d
```

In both cases set `APP_URL=https://…` and `COOKIE_SECURE=true`. Cookies then use the
`__Host-` prefix and browsers will not send them over HTTP.

## 6. Trust stores

| What | Where it validates | How to add a private CA |
|---|---|---|
| RDP server certificates | FreeRDP inside **guacd** | mount a bundle (system CAs + yours) over `/etc/ssl/certs/ca-certificates.crt` in the guacd service (see the comment in `docker-compose.yml`) |
| FTPS, OIDC, Infisical/OpenBao | api container | mount a bundle and set `TLS_CA_BUNDLE=/path/in/container` |
| PostgreSQL | api container | mount the CA and set `DB_SSLROOTCERT`, `DB_SSLMODE=verify-full` |

## 7. Docker secrets instead of environment variables

```yaml
# docker-compose.override.yml
services:
  api:
    secrets: [db_password, encryption_keys, oidc_client_secret]
    environment:
      DB_PASSWORD_FILE: /run/secrets/db_password
      ENCRYPTION_KEYS_FILE: /run/secrets/encryption_keys
      OIDC_CLIENT_SECRET_FILE: /run/secrets/oidc_client_secret
secrets:
  db_password: { file: ./secrets/db_password }
  encryption_keys: { file: ./secrets/encryption_keys }
  oidc_client_secret: { file: ./secrets/oidc_client_secret }
```

## 8. Operations

* Health: `GET /healthz` (liveness) and `GET /readyz` (database and guacd reachability). The
  compose health checks use them.
* Logs: JSON on stdout (`LOG_FORMAT=json`). Secrets are redacted by a logging processor.
  Request logs never include query strings (they contain WebSocket tickets).
* Audit: stored in PostgreSQL (`audit_events`), visible under *Administration → Audit log*,
  and kept for `AUDIT_RETENTION_DAYS`.
* Scaling: run **one** `api` replica (see limitations A1). Size it vertically. A session is
  mostly I/O: plan roughly 30–60 MB RAM per busy RDP session in guacd, and little in the api.

## 9. Upgrading

1. Read the release notes. Back up the database (see [backup-restore.md](backup-restore.md)).
2. `git pull` (or change `IMAGE_TAG`).
3. `docker compose build && docker compose up -d`. Migrations run automatically.
   Live sessions are dropped during the API restart. Clients reconnect on their own, and RDP
   returns to the same Windows session.
4. Check `docker compose ps` and `docker compose logs api | grep -i error`.

Rollback: restore the database backup taken in step 1 and start the previous image tag.
Alembic downgrades exist, but restoring the backup is the supported rollback.

Upgrading guacd: change the tag in `docker-compose.yml`. Parameters that an older or newer guacd
does not advertise are skipped and logged as `guacd_params_unsupported`.

## 10. Uninstall

`docker compose down` keeps the volumes. `docker compose down -v` also removes the RDP drive
and recording volumes. The external database is never touched.
