# Backup and recovery

All persistent state is in the external PostgreSQL database. There are two exceptions: files
uploaded to RDP drive redirection and session recordings, which live in Docker volumes.

| Data | Location | Needed to restore? |
|---|---|---|
| Users, roles, ACLs, folders, connections, credentials (encrypted), settings, audit | PostgreSQL | **yes** |
| Encryption keys (`ENCRYPTION_KEYS`) | `.env` / Docker secret | **yes**, without them the encrypted secrets are lost |
| `.env` / compose overrides | host | yes |
| RDP drive files (`drives` volume) | Docker volume | optional (transient transfer area) |
| Recordings (`recordings` volume) | Docker volume | if recording is enabled |
| Live sessions | API memory | no, they cannot be restored |

**Store the encryption keys separately from the database backups**, for example in your
password manager or KMS. A database backup without the keys cannot reveal secrets, and
the keys alone are useless without the database.

## Backup

```bash
# logical backup (consistent snapshot, can run while the app is up)
PGPASSWORD=… pg_dump --host db.example.com --username connections --format=custom \
  --file connections-$(date +%F).dump connections

# optional volumes
docker run --rm -v <project>_recordings:/v -v "$PWD":/b busybox tar czf /b/recordings-$(date +%F).tgz -C /v .
```

Managed PostgreSQL services (point-in-time recovery, snapshots) are equally fine.
Recommendation: daily dumps, keep 14+, and test a restore every quarter.

## Restore

```bash
docker compose stop api web
# into an empty database (or drop and recreate it first)
PGPASSWORD=… pg_restore --host db.example.com --username connections --dbname connections \
  --clean --if-exists --no-owner connections-2026-10-11.dump
# make sure .env has the SAME ENCRYPTION_KEYS as when the backup was taken
docker compose up -d
docker compose exec api python -m app.cli check-config
```

If the backup is older than the current code, the start-up migration upgrades it automatically.

## Disaster scenarios

| Scenario | Recovery |
|---|---|
| Database lost | restore the latest dump. Changes after it are lost. |
| Encryption key lost | data stays readable, but every stored password, key and provider secret is unusable. Re-enter them, or re-sync from Infisical/OpenBao. Connections, folders and ACLs survive. |
| Key compromised | add a new key, set it active, run `python -m app.cli rotate-keys`, remove the old key, rotate the remote passwords themselves. |
| IdP outage | enable `LOCAL_AUTH_ENABLED` and use the recovery admin. |
| Host lost | new host + same `.env` + database → `docker compose up -d`. |

## Key rotation (routine)

```bash
# 1. add a new key and make it active
ENCRYPTION_KEYS=k1:<old>,k2:<new>
ENCRYPTION_ACTIVE_KEY_ID=k2
docker compose up -d api
# 2. re-encrypt everything with k2
docker compose exec api python -m app.cli rotate-keys
# 3. remove k1 from ENCRYPTION_KEYS and restart
```
