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
provider, path, key and policies; the magnifier button browses the provider (names only, never
values; needs `secrets.manage`). A key without a name is entered as an empty key. *Save & sync*
stores the mapping and synchronises immediately.

## Automatic import (OpenBao)

With *Create credentials automatically* (`auto_import`, or `OPENBAO_AUTO_IMPORT=true` for the
provider from environment variables), every full sync walks the KV mount, or only the part below
*import path* (`OPENBAO_IMPORT_PATH`):

* each secret becomes a credential named after the secret (last path segment);
* its keys are matched to the credential fields by name, ignoring case, `_`, `-` and spaces:

  | Field | Keys |
  |---|---|
  | username | `username`, `user`, `login`, `userid`, `account`, `benutzer`, `benutzername` |
  | password | `password`, `pass`, `passwd`, `pwd`, `secret`, `kennwort`, `passwort` |
  | domain | `domain`, `domäne`, `ntdomain`, `userdomain` |
  | private_key | `private_key`, `ssh_key`, `key`, `id_rsa`, `id_ed25519` |
  | passphrase | `passphrase`, `key_passphrase`, `ssh_passphrase` |

  A secret with a single key that matches none of these, for example a key without a name, is
  taken as the password. Secrets without any usable key are skipped.
* the matched keys become mappings and the sync fills in the values. Keys added to a secret later
  get mappings on the next sync.
* a credential that already has the secret's name, and is not linked to this provider yet, is linked
  instead of creating a second one. Local values that differ become conflicts; nothing is
  overwritten silently.
* credentials are never deleted by the import. When a secret is removed, its mappings show
  `remote_missing`. A deleted auto-imported credential comes back on the next sync while the secret
  still exists; limit the import with *import path* instead.

New credentials belong to the user who started the sync; scheduled syncs create them without an
owner, so only administrators (and explicit grants) see them until permissions are set.

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
