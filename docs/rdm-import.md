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
| `SubConnections`, `ParentID` (e.g. an SFTP entry with `HostSourceMode=Inherited`) | child connections that inherit the parent's hostname |
| `Credential` entries | credential store entries (username/domain); groups of type *Credentials* create no connection folder |
| `CredentialConnectionID`, `CredentialConnectionSavedPath` (`Group\Entry`) | `auth_mode = credential` pointing at the imported entry |
| `CredentialConnectionSavedPath` "prompt on connection" (`--- Bei Verbindung abfragen ---`, …) | choose a credential at connect time; children with the same setting as their parent inherit it |
| `AlwaysAskForPassword` | interactive authentication (typed at connect time) |
| `Url` / `Host`, `Port` | hostname + domain (`web01.example.org` → `web01` / `example.org`), port (`host:port` in Url is split) |
| `Name` `host01 (Mail)` | description "Mail" (the tree shows `hostname (description)`); a name unrelated to the hostname becomes the description |
| `Description`, `Keywords`/`Tags` | description (below the label line), tags |
| RDP `SoundHook=DoNotPlay` | audio disabled |
| RDP `AuthentificationLevel` | `cert_policy` (0/ConnectDontWarnMe → ignore, 2/WarnMe → tofu, 1/DontConnect → validate), reported as a warning |
| RDP `UseCredSSP`, `Console`, `RemoteApplicationProgram`, drive redirection, gateway host | corresponding RDP settings |
| VNC `Display`, view-only; FTP passive/active, remote directory | corresponding settings |

Every other non-empty value is listed per item under **unsupported settings**, in both the
preview and the final report. Nothing is dropped silently, except RDM client bookkeeping that has
no meaning here: owner/resource texts, the `Security` block (offline cache, temporary access),
proxy types without a proxy host, keyboard hook, status and app version. Values of sensitive-looking settings
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
