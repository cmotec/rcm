"""Request/response models of the REST API (v1)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

T = TypeVar("T")

Protocol = Literal["host", "ssh", "sftp", "rdp", "vnc", "ftp", "ftps"]
AuthMode = Literal["inherit", "direct", "credential", "interactive"]
PersonalAccountKind = Literal["user", "privileged"]
PersonalAccountChoice = Literal["any", "user", "privileged"]

# OpenAPI descriptions of the authentication fields (connections, hosts and folders share them).
AUTH_MODE_DOC = (
    "Where the login comes from. `inherit`: from the parent connection (e.g. the host), then from the folder; "
    "`direct`: `username` / `domain` / `password` stored here; `credential`: the credential-store entry "
    "`credential_id`, or without one the user picks a credential when connecting (optionally only from "
    "`credential_folder_id`); `interactive`: asked when connecting (`username` / `domain` pre-fill the prompt). "
    "Fields the mode does not use are cleared. See docs/api.md, 'Authentication'."
)
INHERIT_AUTH_DOC = ("Fill fields that are still empty from the parent connection, then the folder (default true). "
                    "false = this connection's authentication is complete on its own.")
USERNAME_DOC = "auth_mode direct / interactive (pre-fill)"
PASSWORD_DOC = "auth_mode direct; write-only (responses only say `password_set`); null clears"
CREDENTIAL_ID_DOC = ("auth_mode credential: the pinned credential-store entry (the caller needs 'connect' on it). "
                     "null = the user chooses at connect time")
PERSONAL_ACCOUNT_DOC = ("auth_mode credential: log in with the connecting user's own account (Profile → My accounts) "
                        "instead of a credential-store entry: `user` or `privileged` (that account, no question), "
                        "`any` (the user picks one of their accounts). Clears credential_id / credential_folder_id")
PermissionLevel = Literal["view", "connect", "edit", "manage"]
_HOSTNAME = r"^[A-Za-z0-9_.:\-\[\]%]+$"
_DOMAIN = r"^[A-Za-z0-9_.\-]+$"
_COLOR = r"^(#[0-9A-Fa-f]{3,8}|[a-z]{3,20})$"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class Problem(BaseModel):
    type: str
    title: str
    status: int
    code: str
    detail: str | None = None
    request_id: str | None = None


# --------------------------------------------------------------------------- auth & users


class AuthConfigOut(BaseModel):
    oidc_enabled: bool
    oidc_display_name: str
    local_enabled: bool


class LocalLogin(Strict):
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=1024)


class MeOut(BaseModel):
    id: uuid.UUID
    username: str
    email: str | None
    display_name: str | None
    roles: list[str]
    permissions: list[str]
    is_admin: bool
    csrf_token: str | None
    auth_method: str
    session_expires_at: datetime | None
    # Signage account: the UI shows only this connection, fullscreen (null = none assigned yet).
    signage: bool = False
    signage_connection_id: uuid.UUID | None = None


class UserOut(Out):
    id: uuid.UUID
    username: str
    email: str | None
    display_name: str | None
    is_local: bool
    is_active: bool
    last_login_at: datetime | None
    created_at: datetime
    roles: list[str] = []
    groups: list[str] = []
    is_signage: bool = False
    signage_connection_id: uuid.UUID | None = None


_SIGNAGE_HELP = ("signage account: may only open signage_connection_id (an RDP or VNC connection), "
                 "fullscreen without menus; roles and ACLs do not apply")


class UserCreate(Strict):
    username: str = Field(min_length=1, max_length=255)
    email: str | None = Field(None, max_length=320)
    display_name: str | None = Field(None, max_length=255)
    password: str | None = Field(None, min_length=12, max_length=1024, description="local accounts only")
    roles: list[str] = []
    is_signage: bool = Field(False, description=_SIGNAGE_HELP)
    signage_connection_id: uuid.UUID | None = None


class UserUpdate(Strict):
    email: str | None = Field(None, max_length=320)
    display_name: str | None = Field(None, max_length=255)
    is_active: bool | None = None
    roles: list[str] | None = Field(None, description="manually assigned roles (replaces)")
    password: str | None = Field(None, min_length=12, max_length=1024)
    is_signage: bool | None = Field(None, description=_SIGNAGE_HELP)
    signage_connection_id: uuid.UUID | None = None


class RoleOut(Out):
    id: uuid.UUID
    name: str
    description: str | None
    builtin: bool
    permissions: list[str]
    resource_cap: str


class RoleCreate(Strict):
    name: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    description: str | None = Field(None, max_length=1000)
    permissions: list[str] = []
    resource_cap: PermissionLevel = "connect"


class RoleUpdate(Strict):
    description: str | None = Field(None, max_length=1000)
    permissions: list[str] | None = None
    resource_cap: PermissionLevel | None = None


class GroupMappingOut(Out):
    id: uuid.UUID
    group_name: str
    role_id: uuid.UUID


class GroupMappingCreate(Strict):
    group_name: str = Field(min_length=1, max_length=255)
    role: str


class ApiTokenCreate(Strict):
    name: str = Field(min_length=1, max_length=255)
    expires_in_days: int | None = Field(90, ge=1, le=3650)
    read_only: bool = False


class ApiTokenOut(Out):
    id: uuid.UUID
    name: str
    prefix: str
    read_only: bool
    expires_at: datetime | None
    last_used_at: datetime | None
    created_at: datetime
    revoked_at: datetime | None


class ApiTokenCreated(ApiTokenOut):
    token: str = Field(description="shown once")


# --------------------------------------------------------------------------- folders


class FolderBase(Strict):
    icon: str | None = Field(None, max_length=64, pattern=r"^[a-z0-9-]+$")
    color: str | None = Field(None, max_length=32, pattern=_COLOR)
    description: str | None = Field(None, max_length=10000)
    search_domain: str | None = Field(None, max_length=253, pattern=_DOMAIN)
    sort_order: int | None = None
    # authentication for the connections inside whose own authentication inherits
    auth_mode: AuthMode | None = Field(None, description=AUTH_MODE_DOC + " On a folder it applies to the connections "
                                       "inside whose own authentication inherits.")
    username: str | None = Field(None, max_length=255, description=USERNAME_DOC)
    domain: str | None = Field(None, max_length=255, description=USERNAME_DOC)
    password: str | None = Field(None, max_length=4096, description=PASSWORD_DOC)
    credential_id: uuid.UUID | None = Field(None, description=CREDENTIAL_ID_DOC)
    credential_folder_id: uuid.UUID | None = Field(
        None, description="auth_mode credential without credential_id: offer only credentials from this folder")
    personal_account: PersonalAccountChoice | None = Field(None, description=PERSONAL_ACCOUNT_DOC)


class FolderCreate(FolderBase):
    name: str = Field(min_length=1, max_length=255)
    parent_id: uuid.UUID | None = None


class FolderUpdate(FolderBase):
    name: str | None = Field(None, min_length=1, max_length=255)
    parent_id: uuid.UUID | None = None
    version: int | None = None


class FolderOut(Out):
    id: uuid.UUID
    name: str
    parent_id: uuid.UUID | None
    icon: str | None
    color: str | None
    description: str | None
    search_domain: str | None
    sort_order: int
    owner_id: uuid.UUID | None
    version: int
    created_at: datetime
    updated_at: datetime
    permission: str | None = None
    auth_mode: str = "inherit"
    username: str | None = None
    domain: str | None = None
    password_set: bool = False
    credential_id: uuid.UUID | None = None
    credential_folder_id: uuid.UUID | None = None
    personal_account: str | None = None


# --------------------------------------------------------------------------- connections


class ConnectionFields(Strict):
    hostname: str | None = Field(None, max_length=255, pattern=_HOSTNAME)
    port: int | None = Field(None, ge=1, le=65535)
    auth_mode: AuthMode | None = Field(None, description=AUTH_MODE_DOC)
    inherit_auth: bool | None = Field(None, description=INHERIT_AUTH_DOC)
    username: str | None = Field(None, max_length=255, description=USERNAME_DOC)
    domain: str | None = Field(None, max_length=255, description=USERNAME_DOC)
    password: str | None = Field(None, max_length=4096, description=PASSWORD_DOC)
    credential_id: uuid.UUID | None = Field(None, description=CREDENTIAL_ID_DOC)
    search_domain: str | None = Field(None, max_length=253, pattern=_DOMAIN)
    description: str | None = Field(None, max_length=20000)
    tags: list[str] | None = Field(None, max_length=50)
    settings: dict[str, Any] | None = None
    sort_order: int | None = None
    lookups: dict[str, str] | None = Field(
        None, description="field -> {{lookup:<credential name pattern>:<field>}} (see docs/architecture.md)")
    credential_folder_id: uuid.UUID | None = Field(
        None, description="auth_mode credential without credential_id: offer only credentials from this folder")
    personal_account: PersonalAccountChoice | None = Field(None, description=PERSONAL_ACCOUNT_DOC)

    @field_validator("lookups")
    @classmethod
    def _lookups(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        from app.services.lookups import validate_lookups

        return None if v is None else validate_lookups(v)

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        out = []
        for t in v:
            t = t.strip()
            if not t or len(t) > 64:
                raise ValueError("tags must be 1-64 characters")
            if t not in out:
                out.append(t)
        return out


class ConnectionCreate(ConnectionFields):
    name: str = Field(min_length=1, max_length=255)
    protocol: Protocol
    folder_id: uuid.UUID | None = None
    parent_id: uuid.UUID | None = None


class ConnectionUpdate(ConnectionFields):
    """PATCH semantics: omitted fields are unchanged; ``null`` clears a field."""

    name: str | None = Field(None, min_length=1, max_length=255)
    protocol: Protocol | None = None
    version: int | None = Field(None, description="optimistic concurrency check")


class ConnectionMove(Strict):
    folder_id: uuid.UUID | None = None
    parent_id: uuid.UUID | None = None
    sort_order: int | None = None


class ConnectionDuplicate(Strict):
    name: str | None = Field(None, min_length=1, max_length=255)
    include_children: bool = False
    # Where the copy goes (at most one). None of them: next to the original.
    folder_id: uuid.UUID | None = Field(None, description="copy into this folder")
    parent_id: uuid.UUID | None = Field(None, description="copy under this connection (e.g. another host)")
    root: bool = Field(False, description="copy to the top level")


class SecurityWarning(BaseModel):
    code: str
    message: str


class ConnectionOut(Out):
    id: uuid.UUID
    name: str
    protocol: Protocol
    hostname: str | None
    port: int | None
    folder_id: uuid.UUID | None
    parent_id: uuid.UUID | None
    auth_mode: AuthMode
    inherit_auth: bool
    username: str | None
    domain: str | None
    password_set: bool = False
    credential_id: uuid.UUID | None
    search_domain: str | None
    description: str | None
    tags: list[str]
    settings: dict[str, Any]
    sort_order: int
    owner_id: uuid.UUID | None
    version: int
    created_at: datetime
    updated_at: datetime
    permission: str | None = None
    favorite: bool = False
    warnings: list[SecurityWarning] = []
    lookups: dict[str, str] = {}
    credential_folder_id: uuid.UUID | None = None
    personal_account: str | None = None
    # display values of lookup fields (hostname, port, description, …), resolved for the caller
    resolved: dict[str, Any] = {}


class TreeOut(BaseModel):
    folders: list[FolderOut]
    connections: list[ConnectionOut]


# --------------------------------------------------------------------------- credentials


def _check_attributes(v: dict[str, str | None]) -> dict[str, str | None]:
    if len(v) > 100:
        raise ValueError("at most 100 attributes")
    out: dict[str, str | None] = {}
    for k, val in v.items():
        key = k.strip()
        if not key or len(key) > 59 or not all(ch.isalnum() or ch in "_-." for ch in key):
            raise ValueError(f"attribute name '{k}': 1-59 characters, letters, digits, '_', '-', '.'")
        if val is not None and len(str(val)) > 8000:
            raise ValueError(f"attribute '{key}' is too long (max 8000 characters)")
        out[key] = None if val is None else str(val)
    return out


class CredentialFields(Strict):
    username: str | None = Field(None, max_length=255)
    domain: str | None = Field(None, max_length=255)
    password: str | None = Field(None, max_length=4096, description="write-only")
    private_key: str | None = Field(None, max_length=65536, description="write-only, OpenSSH/PEM")
    passphrase: str | None = Field(None, max_length=4096, description="write-only")
    description: str | None = Field(None, max_length=20000)
    metadata: dict[str, Any] | None = None
    rotation_interval_days: int | None = Field(None, ge=1, le=3650)
    folder_id: uuid.UUID | None = None
    attributes: dict[str, str] | None = Field(
        None, description="values for lookups (port, hostname, description, …), encrypted at rest, shown to "
                          "viewers; replaces all attributes")
    secret_attributes: dict[str, str | None] | None = Field(
        None, description="write-only values for lookups (encrypted, never returned): a value sets, null removes; "
                          "names not listed stay unchanged")

    @field_validator("attributes")
    @classmethod
    def _attributes(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        return None if v is None else {k: val for k, val in _check_attributes(v).items() if val is not None}

    @field_validator("secret_attributes")
    @classmethod
    def _secret_attributes(cls, v: dict[str, str | None] | None) -> dict[str, str | None] | None:
        return None if v is None else _check_attributes(v)

    @field_validator("metadata")
    @classmethod
    def _meta(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        import json

        if v is not None and len(json.dumps(v)) > 20000:
            raise ValueError("metadata too large")
        return v


class CredentialCreate(CredentialFields):
    name: str = Field(min_length=1, max_length=255)
    parent_id: uuid.UUID | None = None


class CredentialUpdate(CredentialFields):
    name: str | None = Field(None, min_length=1, max_length=255)
    parent_id: uuid.UUID | None = None
    version: int | None = None


class CredentialOut(Out):
    id: uuid.UUID
    name: str
    parent_id: uuid.UUID | None
    username: str | None
    domain: str | None
    password_set: bool
    private_key_set: bool
    passphrase_set: bool
    private_key_fingerprint: str | None = None
    description: str | None
    metadata: dict[str, Any]
    owner_id: uuid.UUID | None
    rotation_interval_days: int | None
    secret_changed_at: datetime | None
    rotation_due: bool = False
    version: int
    created_at: datetime
    updated_at: datetime
    permission: str | None = None
    external_fields: list[str] = []
    usage_count: int = 0
    folder_id: uuid.UUID | None = None
    attributes: dict[str, str] = {}
    secret_attribute_names: list[str] = []


class CredentialBulkIds(Strict):
    ids: list[uuid.UUID] = Field(min_length=1, max_length=1000)


class CredentialBulkMove(CredentialBulkIds):
    folder_id: uuid.UUID | None = Field(description="target credential folder; null = no folder")


class BulkFailure(BaseModel):
    id: uuid.UUID
    name: str | None = None
    error: str


class BulkResult(BaseModel):
    done: list[uuid.UUID]
    failed: list[BulkFailure]


class CredentialFolderCreate(Strict):
    name: str = Field(min_length=1, max_length=255)
    parent_id: uuid.UUID | None = None
    description: str | None = Field(None, max_length=10000)


class CredentialFolderUpdate(Strict):
    name: str | None = Field(None, min_length=1, max_length=255)
    parent_id: uuid.UUID | None = None
    description: str | None = Field(None, max_length=10000)
    version: int | None = None


class CredentialFolderOut(Out):
    id: uuid.UUID
    name: str
    parent_id: uuid.UUID | None
    description: str | None
    owner_id: uuid.UUID | None
    version: int
    created_at: datetime
    updated_at: datetime
    permission: str | None = None


class CredentialSecrets(BaseModel):
    username: str | None
    domain: str | None
    password: str | None
    private_key: str | None
    passphrase: str | None
    secret_attributes: dict[str, str] | None = None


class CredentialReveal(Strict):
    reason: str | None = Field(None, max_length=500)
    fields: list[Literal["password", "private_key", "passphrase", "secret_attributes"]] = ["password"]


# --------------------------------------------------------------------------- ACL


class AclEntryIn(Strict):
    principal_type: Literal["user", "group", "role"]
    principal_id: uuid.UUID
    permission: PermissionLevel


class AclEntryOut(Out):
    id: uuid.UUID
    resource_type: str
    resource_id: uuid.UUID
    principal_type: str
    principal_id: uuid.UUID
    principal_name: str | None = None
    permission: str


class EffectiveAccessOut(BaseModel):
    resource_type: str
    resource_id: uuid.UUID | None
    user_id: uuid.UUID
    permission: str | None
    global_permissions: list[str]


# --------------------------------------------------------------------------- sessions


class InteractiveCredentials(Strict):
    username: str | None = Field(None, max_length=255)
    password: str | None = Field(None, max_length=4096)
    domain: str | None = Field(None, max_length=255)
    private_key: str | None = Field(None, max_length=65536)
    passphrase: str | None = Field(None, max_length=4096)


class AcceptedHostKey(Strict):
    hostname: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    fingerprint: str = Field(min_length=10, max_length=128)




class AuthOverrideIn(Strict):
    """Open with different credentials (this session only)."""

    mode: Literal["own", "credential", "account"]
    credentials: InteractiveCredentials | None = Field(None, description="mode 'own': typed values, never stored")
    credential_id: uuid.UUID | None = Field(None, description="mode 'credential': a credential the user may use")
    account: PersonalAccountKind | None = Field(None, description="mode 'account': one of the user's personal accounts")


class PersonalAccountIn(Strict):
    username: str | None = Field(None, max_length=255)
    domain: str | None = Field(None, max_length=255)
    password: str | None = Field(None, max_length=4096, description="new password; omitted or empty = unchanged")
    clear_password: bool = False


class PersonalAccountOut(BaseModel):
    kind: PersonalAccountKind
    username: str | None = None
    domain: str | None = None
    has_password: bool = False
    updated_at: datetime | None = None


class SessionCreate(Strict):
    connection_id: uuid.UUID
    credentials: InteractiveCredentials | None = None
    credential_id: uuid.UUID | None = Field(
        None, description="credential chosen at connect time (connections in credential mode without a pinned credential)")
    accept_host_keys: list[AcceptedHostKey] = Field(default_factory=list, max_length=4)
    auth_override: AuthOverrideIn | None = None


class SessionOut(BaseModel):
    id: uuid.UUID
    connection_id: uuid.UUID | None
    connection_name: str
    protocol: str
    kind: Literal["guac", "terminal", "files"]
    state: str
    created_at: datetime
    connected_at: datetime | None
    last_activity_at: datetime
    effective_host: str | None
    effective_port: int | None
    host_resolution_note: str | None = None
    warnings: list[SecurityWarning] = []
    auth_sources: dict[str, Any] = {}
    owner_id: uuid.UUID
    owner_name: str | None = None
    error: str | None = None
    ticket: str | None = None
    ticket_expires_at: datetime | None = None
    tunnel_path: str | None = None
    client_settings: dict[str, Any] = {}


class PromptRequiredOut(BaseModel):
    prompt_required: bool = True
    fields: list[str]
    username: str | None = None
    domain: str | None = None


# --------------------------------------------------------------------------- files


class FileEntry(BaseModel):
    name: str
    path: str
    type: Literal["file", "dir", "link", "other"]
    size: int | None = None
    modified: datetime | None = None
    permissions: str | None = None
    mode: int | None = None
    owner: str | None = None
    group: str | None = None
    link_target: str | None = None


class DirListing(BaseModel):
    path: str
    entries: list[FileEntry]


class MkdirIn(Strict):
    path: str = Field(min_length=1, max_length=4096)


class RenameIn(Strict):
    source: str = Field(min_length=1, max_length=4096)
    target: str = Field(min_length=1, max_length=4096)
    overwrite: bool = False


class DeleteIn(Strict):
    path: str = Field(min_length=1, max_length=4096)
    recursive: bool = False


class ChmodIn(Strict):
    path: str = Field(min_length=1, max_length=4096)
    mode: int = Field(ge=0, le=0o7777)


# --------------------------------------------------------------------------- known hosts


class KnownHostOut(Out):
    id: uuid.UUID
    hostname: str
    port: int
    key_type: str
    fingerprint: str
    public_key: str
    added_by: uuid.UUID | None
    created_at: datetime
    last_seen_at: datetime | None


class KnownHostCreate(Strict):
    hostname: str = Field(min_length=1, max_length=255)
    port: int = Field(22, ge=1, le=65535)
    public_key: str = Field(min_length=10, max_length=16384, description="OpenSSH public key line")


# --------------------------------------------------------------------------- audit & settings


class AuditEventOut(Out):
    id: int
    ts: datetime
    actor_id: uuid.UUID | None
    actor_name: str | None
    action: str
    resource_type: str | None
    resource_id: str | None
    outcome: str
    ip: str | None
    request_id: str | None
    details: dict[str, Any]


class PreferencesIn(BaseModel):
    data: dict[str, Any]

    @field_validator("data")
    @classmethod
    def _size(cls, v: dict[str, Any]) -> dict[str, Any]:
        import json

        if len(json.dumps(v)) > 512_000:
            raise ValueError("preferences too large")
        return v


class AppSettingsOut(BaseModel):
    values: dict[str, Any]
    defaults: dict[str, Any]


class AppSettingsIn(Strict):
    rdp_cert_policy_default: Literal["validate", "fingerprints", "tofu", "ignore"] | None = None
    rdp_cert_allow_insecure: bool | None = None
    ssh_host_key_policy_default: Literal["prompt", "strict", "accept-new"] | None = None
    ftp_tls_verify_default: bool | None = None
    banner_message: str | None = Field(None, max_length=2000)
