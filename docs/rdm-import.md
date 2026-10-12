# Import from Devolutions Remote Desktop Manager

*Administration → Import (RDM)*, or `POST /api/v1/imports/rdm` followed by `POST /api/v1/imports/{id}/apply`.

## Supported exports

| Format | How to create it in RDM | Notes |
|---|---|---|
| `.rdm` (XML) | *File → Export → Export selected/all entries → Remote Desktop Manager (*.rdm)* | preferred: folders, sub-connections, credential entries, protocol settings |
| CSV | *File → Export → CSV* | columns mapped by header name (Name, ConnectionType, Group, Host/URL, Port, Username, Domain, Description, Tags/Keywords); other columns are reported |

Export **without** an export password if possible. Exported passwords are encrypted either way
and are not imported (see below).

## Mapping

| RDM | Imported as |
|---|---|
| Group / Folder entries, `Group` path (`A\B`) | folders (created under the chosen target folder, reusing existing names) |
| `RDPConfigured` | RDP connection |
| `SSHShell` | SSH connection |
| `VNC`, `AppleRemoteDesktop` | VNC connection |
| `FTP` (+ `Encryption` explicit/implicit) | FTP or FTPS connection |
| `SFTP` | SFTP connection |
| `SubConnections` | child connections (parent/child hierarchy) |
| `Credential` entries | credential store entries (username/domain) |
| `CredentialConnectionID` | `auth_mode = credential` pointing at the imported entry |
| `Url` / `Host`, `Port` | hostname / port (`host:port` in Url is split) |
| `Description`, `Keywords`/`Tags` | description, tags |
| RDP `AuthentificationLevel` | `cert_policy` (0/ConnectDontWarnMe → ignore, 2/WarnMe → tofu, 1/DontConnect → validate), reported as a warning |
| RDP `UseCredSSP`, `Console`, `RemoteApplicationProgram`, drive redirection, gateway host | corresponding RDP settings |
| VNC `Display`, view-only; FTP passive/active, remote directory | corresponding settings |

Every other non-empty value is listed per item under **unsupported settings**, in both the
preview and the final report. Nothing is dropped silently. Values of sensitive-looking settings
are shown as `[redacted]`. Unsupported entry types (web, VPN, documents, PowerShell, …) are
listed and skipped.

## Workflow

1. Upload. The file is parsed with a safe XML parser (no DTDs or entities, size limit
   `IMPORT_MAX_FILE_MB`).
2. Preview: each entry with its type, target, folder, credential status, warnings and
   unsupported settings. **Duplicates** are detected (same RDM entry imported before, or the same
   protocol + host + port as an existing connection) and are deselected by default.
3. Select entries, choose the target folder and the duplicate handling (*skip* / *import anyway*).
4. Apply: each item is imported in its own savepoint, so one failure does not abort the rest.
5. Report: created counts, skipped items with reasons, errors, warnings, unmapped settings and
   the **manual credential migration** list.

## Passwords

RDM stores passwords encrypted (`SafePassword`, data-source or export keys). The importer **does
not attempt to decrypt them or bypass that protection.** Affected connections are imported without
a password and listed in the report. To migrate them, do one of the following:

1. Create the credentials in the credential store (or map them to Infisical/OpenBao), then set
   the connection's authentication to *Credential store*.
2. Or switch the connection to *Ask when connecting*, so users type the password at launch.
3. Private-key files referenced by path in RDM live on the old client machine. Paste the keys into
   credential entries.
