# Deployment

## 1. What gets deployed

| Container | Image | Purpose | Exposed |
|---|---|---|---|
| `web` | `remote-connections-manager-web`, from `frontend/` (nginx-unprivileged) | serves the SPA, reverse-proxies `/api` (incl. WebSockets) | published port (`HTTP_PORT`, default 8080) |
| `api` | `remote-connections-manager-api`, from `backend/` (python:3.12-slim, uid 10001) | REST API, WebSocket tunnels, SSH/SFTP/FTP, secret sync | internal only |
| `guacd` | `remote-connections-manager-guacd`, from `guacd/` (`guacamole/guacd:1.6.0` + data directories owned by guacd, uid 1000) | RDP/VNC (and optional SSH) protocol gateway | **never published** |

guacd runs unprivileged. Its image pre-creates `/drives` and `/recordings` owned by the guacd user,
and Docker copies that owner into the `drives` and `recordings` volumes when it creates them, so
no root init container is needed. Upgrading from a version with the one-shot `volume-init`
container: the existing volumes are already owned by uid 1000, and the leftover stopped container
can be removed (`docker compose up -d --remove-orphans`, or delete it in Arcane).

Networks: `app` (web ↔ api) and `gateway` (api ↔ guacd). guacd is not on the `app`
network and has no published port. The api and guacd containers need outbound access to
the remote hosts, and the api container also needs outbound access to PostgreSQL, the IdP and the
secret managers.

PostgreSQL 14 or newer: either your own (`docker-compose.yml`, configured with `DB_*`), or the
bundled one in `docker-compose.postgres.yml`, a complete deployment including PostgreSQL:

```bash
cp .env.example .env    # APP_URL, ENCRYPTION_KEYS, DB_PASSWORD, OIDC_* or LOCAL_*
docker compose -f docker-compose.postgres.yml pull
docker compose -f docker-compose.postgres.yml up -d
```

There, `DB_HOST`/`DB_SSLMODE` are set by the file, `DB_NAME` and `DB_USER` default to `rcm`, and the
data lives in the volume `pgdata` (back it up, see docs/backup-restore.md).

## 2. Prerequisites

* Docker Engine 24+ with Compose v2.20+ (`extends` with `depends_on`, optional `env_file`).
* An empty PostgreSQL database and a role that owns it:
  ```sql
  CREATE ROLE connections LOGIN PASSWORD '…';
  CREATE DATABASE connections OWNER connections;
  ```
* An OIDC client in Authentik (see §4) or a local recovery admin.
* A DNS name and a TLS-terminating reverse proxy (see §5).

### Images and compose files

| File | Images | Use |
|---|---|---|
| `docker-compose.yml` | pulled from a registry | production |
| `docker-compose.dev.yml` | built from this repository | development, testing |

`docker-compose.yml` runs `${IMAGE_PREFIX}/remote-connections-manager-{web,api,guacd}:${IMAGE_TAG}`
(defaults: `ghcr.io/cmotec`, `latest`). Set `IMAGE_PREFIX` in `.env` for another registry or owner,
for example `<gitea-host>/<owner>`, and `IMAGE_TAG` to pin a release (`1.2.0`, `1.2`, `1`); `latest`
follows every new release. Private registry: `docker login <registry>` on the host first. Updating:
`docker compose pull && docker compose up -d`.

### Releases

Images are built only for releases. Work is committed and pushed on `dev` (nothing is built); a
release is a version tag on `main`:

```bash
git checkout main && git pull
git merge dev && git push        # still nothing is built
git tag v1.2.0
git push origin v1.2.0           # builds and publishes the images
git checkout dev
```

The workflow `.github/workflows/container-images.yml` then

1. checks that the tag looks like `vMAJOR.MINOR.PATCH` and that the tagged commit is on `main`
   (otherwise it stops without building),
2. builds `remote-connections-manager-{api,web,guacd}` for amd64 and arm64,
3. pushes them with the tags `1.2.0`, `1.2`, `1` and `latest`. The API reports the version in
   `/api/v1/system/info` and the API docs.

A pre-release such as `v1.3.0-rc1` is published only as `1.3.0-rc1` and does not move `latest`.
`latest` is the last final release built, so tagging a fix for an older line (`v1.1.5` after
`v1.2.0`) moves it back; pin `IMAGE_TAG` in production if that matters. "Run workflow" in the
Actions tab rebuilds an existing tag.

Registries:

* **GitHub:** to `ghcr.io/<owner>/…` with the built-in token, nothing to configure. New packages
  are private; make them public in the package settings if wanted.
* **Gitea** (Actions enabled, a runner with Docker): the same workflow file works. Set the repository
  variable `REGISTRY` (your Gitea host, e.g. `git.example.com`), optionally `REGISTRY_USER`, and the
  secret `REGISTRY_TOKEN` (an access token with the `write:package` scope). Images go to
  `<gitea-host>/<owner>/…`.

`docker-compose.dev.yml` builds the images instead (`docker compose -f docker-compose.dev.yml up -d
--build`) and reuses everything else from `docker-compose.yml` through `extends`.

### Docker management UIs (Arcane, Portainer, …)

Production: point the stack at `docker-compose.yml`; the tool pulls the images. Development: use a
Git-synced project with `docker-compose.dev.yml` as the compose file, because the whole repository
is the build context. Its services set `pull_policy: build`, so the tool builds them instead of
trying a registry pull (Arcane's *Default Deploy Pull Policy* `missing` would otherwise pull the
`local/…` names and fail). `.env` is optional: set the variables from `.env.example` in the tool's
environment editor instead.

## 3. First start

```bash
cp .env.example .env
# generate the encryption key (store a copy in your password manager / KMS!)
echo "ENCRYPTION_KEYS=k1:$(openssl rand -base64 32)"
#   → put that line into .env together with ENCRYPTION_ACTIVE_KEY_ID=k1
$EDITOR .env          # APP_URL, DB_*, OIDC_*, …
docker compose pull
docker compose run --rm --no-deps api python -m app.cli check-config
docker compose up -d
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

The web container serves plain HTTP on port 8080 (published as `HTTP_PORT`). Terminate TLS at a
reverse proxy (Traefik, Caddy, Nginx Proxy Manager, HAProxy, …): forward to the web container,
keep the `Host` header, allow WebSocket upgrades, and use long read timeouts (≥ 1 h, remote
sessions are long-lived WebSockets).

Set `APP_URL=https://…` and `COOKIE_SECURE=true`. Cookies then use the `__Host-` prefix and
browsers will not send them over HTTP.

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
2. Set the new `IMAGE_TAG` (or keep `latest`) and run `docker compose pull`; with
   `docker-compose.dev.yml`: `git pull` and `--build`.
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
