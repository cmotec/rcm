# External secret managers (Infisical, OpenBao)

Credential entries can take individual fields (username, password, domain, private key,
passphrase) from an external secret manager. Synchronisation is optional, runs one way
(external → local) and never silently overwrites local changes.

## Providers

| Provider | Auth | Secret reference (`external_ref`) |
|---|---|---|
| Infisical | Universal Auth (client ID + secret) | `{"path": "/folder", "key": "SECRET_NAME", "environment": "prod"?}` (environment defaults to the provider's) |
| OpenBao (Vault-compatible) | token or AppRole | `{"path": "apps/db", "key": "password"}`: KV v2 at `<mount>/data/<path>` (or KV v1) |

Providers are added under *Administration → Secret providers*, or through environment
variables (`INFISICAL_*`, `OPENBAO_*`). Environment-configured providers are read-only in the
UI. Provider credentials are encrypted at rest. *Test* checks authentication, and *Browse* lists
secret names (never values).

Adding another provider: implement `SecretProviderClient` (`test`, `get`, `browse`) in
`backend/app/services/secrets/` and register it in `PROVIDERS` (plus the `type` check constraint).

## Mapping

In the credential store, open a credential → *External secrets*. Add one row per field: the
provider, path, key and policies. *Save & sync* stores the mapping and synchronises immediately.

## Synchronisation and conflicts

The scheduler runs every `sync_interval_seconds` per provider (`SECRET_SYNC_ENABLED`). You can
also start a sync manually. A PostgreSQL advisory lock prevents overlapping runs.

For each mapping the fingerprints of the remote value and the local value at the last
successful sync are stored. Then:

| Remote | Local (since last sync) | Result |
|---|---|---|
| unchanged | unchanged | nothing |
| changed | unchanged | remote value applied |
| unchanged | edited locally | local edit kept, no conflict |
| changed | edited locally | **conflict** |
| exists | first sync, a different local value exists | **conflict** |
| deleted | – | status `remote_missing`; with `delete_policy=clear` the local field is cleared only if it was not edited locally, otherwise it is kept |

Conflict policy per mapping:

* `manual` (default): nothing is overwritten. The new remote value is held encrypted, and the
  mapping shows **conflict** until someone with *manage* on the credential chooses
  *Use remote* or *Keep local*.
* `prefer_remote`: remote wins (still logged as a conflict override).
* `prefer_local`: local wins, and the remote fingerprint is recorded so the conflict does not repeat.

Every run and every changed mapping is audited (`secret_sync.run`, `secret_sync.mapping`,
`secret_sync.resolve`). Values never appear in the audit log, only fingerprints and statuses.
Provider status (`ok`, `partial`, `error`), last run and conflict count are shown in the UI.

Write-back to the external manager is not implemented (see limitations S3).
