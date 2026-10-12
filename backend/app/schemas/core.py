"""Request/response models of the REST API (v1)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

T = TypeVar("T")

Protocol = Literal["host", "ssh", "sftp", "rdp", "vnc", "ftp", "ftps"]
AuthMode = Literal["inherit", "direct", "credential", "interactive"]
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
    app_display_name: str
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


class UserCreate(Strict):
    username: str = Field(min_length=1, max_length=255)
    email: str | None = Field(None, max_length=320)
    display_name: str | None = Field(None, max_length=255)
    password: str | None = Field(None, min_length=12, max_length=1024, description="local accounts only")
    roles: list[str] = []


class UserUpdate(Strict):
    email: str | None = Field(None, max_length=320)
    display_name: str | None = Field(None, max_length=255)
    is_active: bool | None = None
    roles: list[str] | None = Field(None, description="manually assigned roles (replaces)")
    password: str | None = Field(None, min_length=12, max_length=1024)


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


# --------------------------------------------------------------------------- connections


class ConnectionFields(Strict):
    hostname: str | None = Field(None, max_length=255, pattern=_HOSTNAME)
    port: int | None = Field(None, ge=1, le=65535)
    auth_mode: AuthMode | None = None
    inherit_auth: bool | None = None
    username: str | None = Field(None, max_length=255)
    domain: str | None = Field(None, max_length=255)
    password: str | None = Field(None, max_length=4096, description="write-only")
    credential_id: uuid.UUID | None = None
    search_domain: str | None = Field(None, max_length=253, pattern=_DOMAIN)
    description: str | None = Field(None, max_length=20000)
    tags: list[str] | None = Field(None, max_length=50)
    settings: dict[str, Any] | None = None
    sort_order: int | None = None

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


class TreeOut(BaseModel):
    folders: list[FolderOut]
    connections: list[ConnectionOut]


# --------------------------------------------------------------------------- credentials


class CredentialFields(Strict):
    username: str | None = Field(None, max_length=255)
    domain: str | None = Field(None, max_length=255)
    password: str | None = Field(None, max_length=4096, description="write-only")
    private_key: str | None = Field(None, max_length=65536, description="write-only, OpenSSH/PEM")
    passphrase: str | None = Field(None, max_length=4096, description="write-only")
    description: str | None = Field(None, max_length=20000)
    metadata: dict[str, Any] | None = None
    rotation_interval_days: int | None = Field(None, ge=1, le=3650)

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


class CredentialSecrets(BaseModel):
    username: str | None
    domain: str | None
    password: str | None
    private_key: str | None
    passphrase: str | None


class CredentialReveal(Strict):
    reason: str | None = Field(None, max_length=500)
    fields: list[Literal["password", "private_key", "passphrase"]] = ["password"]


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


class SessionCreate(Strict):
    connection_id: uuid.UUID
    credentials: InteractiveCredentials | None = None
    credential_id: uuid.UUID | None = Field(
        None, description="credential chosen at connect time (connections in credential mode without a pinned credential)")
    accept_host_keys: list[AcceptedHostKey] = Field(default_factory=list, max_length=4)


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
