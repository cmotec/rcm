"""SQLAlchemy models — the PostgreSQL schema of the application.

Conventions:
* UUID primary keys generated in Python (needed before INSERT for the AAD of
  encrypted columns).
* ``*_enc`` columns hold AES-GCM ciphertext (see ``app.security.crypto``).
* Enumerations are ``varchar`` with CHECK constraints (cheap to migrate).
* ``version`` columns implement optimistic concurrency for editable resources.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

PROTOCOLS = ("host", "ssh", "sftp", "rdp", "vnc", "ftp", "ftps")
AUTH_MODES = ("inherit", "direct", "credential", "interactive")
PERMISSIONS = ("view", "connect", "edit", "manage")
RESOURCE_TYPES = ("folder", "connection", "credential", "cred_folder")
PRINCIPAL_TYPES = ("user", "group", "role")
SECRET_FIELDS = ("username", "password", "domain", "private_key", "passphrase")
#: A user's own logins, kept in their profile (see PersonalAccount).
PERSONAL_ACCOUNT_KINDS = ("user", "privileged")
#: auth_mode "credential" with the connecting user's own account: "any" (the user picks one of their
#: accounts), "user" or "privileged" (that one, without asking)
PERSONAL_ACCOUNT_CHOICES = ("any", "user", "privileged")
#: Secret mappings may also fill a credential attribute: field "attr:<name>" (e.g. attr:port),
#: or a secret attribute: "secret:<name>" (write-only, like a password).
ATTRIBUTE_FIELD_PREFIX = "attr:"
SECRET_ATTRIBUTE_FIELD_PREFIX = "secret:"


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def utcnow_sql() -> Any:
    return func.now()


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)
    type_annotation_map = {
        uuid.UUID: UUID(as_uuid=True),
        datetime: DateTime(timezone=True),
        dict[str, Any]: JSONB,
        list[str]: ARRAY(Text),
    }


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(server_default=utcnow_sql())
    updated_at: Mapped[datetime] = mapped_column(server_default=utcnow_sql(), onupdate=utcnow_sql())


# --------------------------------------------------------------------------- identity


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("oidc_issuer", "oidc_subject", name="oidc_identity"),
        Index("uq_users_username_lower", text("lower(username)"), unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(320))
    display_name: Mapped[str | None] = mapped_column(String(255))
    oidc_issuer: Mapped[str | None] = mapped_column(String(512))
    oidc_subject: Mapped[str | None] = mapped_column(String(255))
    password_hash: Mapped[str | None] = mapped_column(String(255))
    is_local: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None]
    # Signage account: may only open this one connection, sees it fullscreen and nothing else.
    # Kept as a separate flag so deleting the connection never turns the account into a normal user.
    is_signage: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    signage_connection_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("connections.id", ondelete="SET NULL")
    )


class Role(TimestampMixin, Base):
    """A role grants global permissions and caps resource permissions.

    ``permissions`` holds global permission strings (``"*"`` = everything and
    bypass of resource ACLs). ``resource_cap`` is the highest resource-level
    permission a member can get from ACLs or ownership.
    """

    __tablename__ = "roles"
    __table_args__ = (CheckConstraint(_in("resource_cap", PERMISSIONS), name="resource_cap"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    permissions: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    resource_cap: Mapped[str] = mapped_column(String(16), default="manage")


class UserRole(Base):
    __tablename__ = "user_roles"
    __table_args__ = (CheckConstraint(_in("source", ("manual", "oidc", "bootstrap")), name="source"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True)
    source: Mapped[str] = mapped_column(String(16), default="manual")


class Group(TimestampMixin, Base):
    __tablename__ = "groups"
    __table_args__ = (CheckConstraint(_in("source", ("oidc", "local")), name="source"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    source: Mapped[str] = mapped_column(String(16), default="oidc")


class UserGroup(Base):
    __tablename__ = "user_groups"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True)


class GroupRoleMapping(Base):
    """IdP group name → application role (in addition to env-configured mappings)."""

    __tablename__ = "group_role_mappings"
    __table_args__ = (UniqueConstraint("group_name", "role_id", name="group_role"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    group_name: Mapped[str] = mapped_column(String(255))
    role_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("roles.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(server_default=utcnow_sql())


class WebSession(Base):
    """Browser login session. ``id`` is the SHA-256 of the cookie value."""

    __tablename__ = "web_sessions"
    __table_args__ = (Index("ix_web_sessions_user", "user_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    csrf_token: Mapped[str] = mapped_column(String(64))
    auth_method: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(server_default=utcnow_sql())
    last_seen_at: Mapped[datetime] = mapped_column(server_default=utcnow_sql())
    expires_at: Mapped[datetime]
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    oidc_id_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    oidc_refresh_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    oidc_revalidate_at: Mapped[datetime | None]


class ApiToken(Base):
    __tablename__ = "api_tokens"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    prefix: Mapped[str] = mapped_column(String(16))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    read_only: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime | None]
    last_used_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=utcnow_sql())


# --------------------------------------------------------------------------- tree


class Folder(TimestampMixin, Base):
    __tablename__ = "folders"
    __table_args__ = (
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="not_own_parent"),
        CheckConstraint(_in("auth_mode", AUTH_MODES), name="auth_mode"),
        Index("ix_folders_parent", "parent_id"),
        Index("ix_folders_credential", "credential_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("folders.id", ondelete="RESTRICT"))
    icon: Mapped[str | None] = mapped_column(String(64))
    color: Mapped[str | None] = mapped_column(String(32))
    description: Mapped[str | None] = mapped_column(Text)
    search_domain: Mapped[str | None] = mapped_column(String(253))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    # Authentication for the connections inside (and in sub-folders) whose own authentication inherits;
    # same modes as connections. "inherit" = take it from the parent folder.
    auth_mode: Mapped[str] = mapped_column(String(16), default="inherit", server_default="inherit")
    username: Mapped[str | None] = mapped_column(String(255))
    domain: Mapped[str | None] = mapped_column(String(255))
    password_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    credential_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("credentials.id", ondelete="RESTRICT"))
    credential_folder_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("credential_folders.id", ondelete="SET NULL")
    )
    # auth_mode "credential": the connecting user's own account instead of a credential-store entry
    personal_account: Mapped[str | None] = mapped_column(String(16))


class Connection(TimestampMixin, Base):
    __tablename__ = "connections"
    __table_args__ = (
        CheckConstraint(_in("protocol", PROTOCOLS), name="protocol"),
        CheckConstraint(_in("auth_mode", AUTH_MODES), name="auth_mode"),
        CheckConstraint("port IS NULL OR (port BETWEEN 1 AND 65535)", name="port_range"),
        CheckConstraint("NOT (parent_id IS NOT NULL AND folder_id IS NOT NULL)", name="single_container"),
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="not_own_parent"),
        Index("ix_connections_folder", "folder_id"),
        Index("ix_connections_parent", "parent_id"),
        Index("ix_connections_credential", "credential_id"),
        Index("ix_connections_tags", "tags", postgresql_using="gin"),
        Index("ix_connections_name_lower", text("lower(name)")),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255))
    protocol: Mapped[str] = mapped_column(String(16))
    hostname: Mapped[str | None] = mapped_column(String(255))
    port: Mapped[int | None] = mapped_column(Integer)
    folder_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("folders.id", ondelete="RESTRICT"))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("connections.id", ondelete="RESTRICT"))
    auth_mode: Mapped[str] = mapped_column(String(16), default="inherit")
    inherit_auth: Mapped[bool] = mapped_column(Boolean, default=True)
    username: Mapped[str | None] = mapped_column(String(255))
    domain: Mapped[str | None] = mapped_column(String(255))
    password_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    credential_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("credentials.id", ondelete="RESTRICT")
    )
    search_domain: Mapped[str | None] = mapped_column(String(253))
    description: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    # field -> "{{lookup:<credential name pattern>:<field>}}", resolved from the credential store at use time
    lookups: Mapped[dict[str, str]] = mapped_column(JSONB, default=dict, server_default="{}")
    # auth_mode "credential" without a pinned credential: offer only credentials from this folder (and below)
    credential_folder_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("credential_folders.id", ondelete="SET NULL")
    )
    # auth_mode "credential": the connecting user's own account instead of a credential-store entry
    personal_account: Mapped[str | None] = mapped_column(String(16))
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    import_source: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    version: Mapped[int] = mapped_column(Integer, default=1)


class Favorite(Base):
    __tablename__ = "favorites"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("connections.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(server_default=utcnow_sql())


# --------------------------------------------------------------------------- credentials


class CredentialFolder(TimestampMixin, Base):
    """Folder tree of the credential store (separate from connection folders); grants inherit down."""

    __tablename__ = "credential_folders"
    __table_args__ = (
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="not_own_parent"),
        Index("ix_credential_folders_parent", "parent_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("credential_folders.id", ondelete="RESTRICT"))
    description: Mapped[str | None] = mapped_column(Text)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    version: Mapped[int] = mapped_column(Integer, default=1)


class Credential(TimestampMixin, Base):
    __tablename__ = "credentials"
    __table_args__ = (
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="not_own_parent"),
        Index("ix_credentials_parent", "parent_id"),
        Index("ix_credentials_folder", "folder_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("credentials.id", ondelete="RESTRICT"))
    username: Mapped[str | None] = mapped_column(String(255))
    domain: Mapped[str | None] = mapped_column(String(255))
    password_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    private_key_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    passphrase_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    description: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict, server_default="{}")
    folder_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("credential_folders.id", ondelete="RESTRICT"))
    # Values connections can take over through lookups, both encrypted at rest (JSON objects, see
    # services/credential_attributes.py): attributes are shown to viewers (port, hostname, description,
    # …); secret attributes are write-only like the password and only reach sessions.
    attributes_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    secret_attributes_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    rotation_interval_days: Mapped[int | None] = mapped_column(Integer)
    secret_changed_at: Mapped[datetime | None]
    version: Mapped[int] = mapped_column(Integer, default=1)


class PersonalAccount(TimestampMixin, Base):
    """A user's own logins (profile): their normal user account and their privileged (admin) account.

    Only the owner can use them, when opening a connection ("My accounts"). The password is
    write-only and encrypted like credential passwords.
    """

    __tablename__ = "personal_accounts"
    __table_args__ = (
        CheckConstraint(_in("kind", PERSONAL_ACCOUNT_KINDS), name="kind"),
        UniqueConstraint("user_id", "kind", name="uq_personal_accounts_user_kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))
    username: Mapped[str | None] = mapped_column(String(255))
    domain: Mapped[str | None] = mapped_column(String(255))
    password_enc: Mapped[bytes | None] = mapped_column(LargeBinary)


class AclEntry(Base):
    __tablename__ = "acl_entries"
    __table_args__ = (
        CheckConstraint(_in("resource_type", RESOURCE_TYPES), name="resource_type"),
        CheckConstraint(_in("principal_type", PRINCIPAL_TYPES), name="principal_type"),
        CheckConstraint(_in("permission", PERMISSIONS), name="permission"),
        UniqueConstraint("resource_type", "resource_id", "principal_type", "principal_id", name="grant"),
        Index("ix_acl_principal", "principal_type", "principal_id"),
        Index("ix_acl_resource", "resource_type", "resource_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    resource_type: Mapped[str] = mapped_column(String(16))
    resource_id: Mapped[uuid.UUID]
    principal_type: Mapped[str] = mapped_column(String(16))
    principal_id: Mapped[uuid.UUID]
    permission: Mapped[str] = mapped_column(String(16))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(server_default=utcnow_sql())


class KnownHost(Base):
    """Trusted SSH host keys (trust on first use, or pinned by an administrator)."""

    __tablename__ = "known_hosts"
    __table_args__ = (UniqueConstraint("hostname", "port", "key_type", name="host_key"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    hostname: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer)
    key_type: Mapped[str] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(128))
    public_key: Mapped[str] = mapped_column(Text)
    added_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(server_default=utcnow_sql())
    last_seen_at: Mapped[datetime | None]


# --------------------------------------------------------------------------- external secrets


class SecretProvider(TimestampMixin, Base):
    __tablename__ = "secret_providers"
    __table_args__ = (
        CheckConstraint(_in("type", ("openbao",)), name="type"),
        CheckConstraint(_in("last_sync_status", ("never", "ok", "partial", "error")), name="sync_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    type: Mapped[str] = mapped_column(String(32))
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    secret_config_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    managed_by_env: Mapped[bool] = mapped_column(Boolean, default=False)
    sync_interval_seconds: Mapped[int] = mapped_column(Integer, default=900)
    last_sync_at: Mapped[datetime | None]
    last_sync_status: Mapped[str] = mapped_column(String(16), default="never")
    last_sync_error: Mapped[str | None] = mapped_column(Text)


class SecretMapping(TimestampMixin, Base):
    """Maps one credential field to one external secret."""

    __tablename__ = "secret_mappings"
    __table_args__ = (
        CheckConstraint(f"{_in('field', SECRET_FIELDS)} OR field LIKE 'attr:%' OR field LIKE 'secret:%'",
                        name="field"),
        CheckConstraint(
            _in("conflict_policy", ("manual", "prefer_remote", "prefer_local")), name="conflict_policy"
        ),
        CheckConstraint(
            _in("delete_policy", ("keep", "clear")), name="delete_policy"
        ),
        CheckConstraint(
            _in("status", ("pending", "ok", "conflict", "error", "remote_missing")), name="status"
        ),
        UniqueConstraint("credential_id", "field", name="credential_field"),
        Index("ix_secret_mappings_provider", "provider_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    credential_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("credentials.id", ondelete="CASCADE"))
    provider_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("secret_providers.id", ondelete="CASCADE"))
    field: Mapped[str] = mapped_column(String(64))
    external_ref: Mapped[dict[str, Any]] = mapped_column(JSONB)
    conflict_policy: Mapped[str] = mapped_column(String(16), default="manual")
    delete_policy: Mapped[str] = mapped_column(String(16), default="keep")
    status: Mapped[str] = mapped_column(String(16), default="pending")
    message: Mapped[str | None] = mapped_column(Text)
    remote_hash: Mapped[str | None] = mapped_column(String(64))
    local_hash: Mapped[str | None] = mapped_column(String(64))
    last_synced_at: Mapped[datetime | None]
    pending_remote_enc: Mapped[bytes | None] = mapped_column(LargeBinary)


class SyncRun(Base):
    __tablename__ = "sync_runs"
    __table_args__ = (Index("ix_sync_runs_provider_started", "provider_id", "started_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    provider_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("secret_providers.id", ondelete="CASCADE"))
    trigger: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(server_default=utcnow_sql())
    finished_at: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(String(16), default="running")
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    error: Mapped[str | None] = mapped_column(Text)


# --------------------------------------------------------------------------- audit & sessions


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint(_in("outcome", ("success", "failure", "denied")), name="outcome"),
        Index("ix_audit_ts", "ts"),
        Index("ix_audit_actor_ts", "actor_id", "ts"),
        Index("ix_audit_resource", "resource_type", "resource_id"),
        Index("ix_audit_action", "action"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ts: Mapped[datetime] = mapped_column(server_default=utcnow_sql())
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    actor_name: Mapped[str | None] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(64))
    resource_type: Mapped[str | None] = mapped_column(String(32))
    resource_id: Mapped[str | None] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(16), default="success")
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    request_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class SessionRecord(Base):
    """Metadata of a remote session (never contains secrets)."""

    __tablename__ = "session_records"
    __table_args__ = (
        Index("ix_session_records_user_started", "user_id", "started_at"),
        Index("ix_session_records_open", "ended_at", postgresql_where=text("ended_at IS NULL")),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    connection_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("connections.id", ondelete="SET NULL")
    )
    connection_name: Mapped[str] = mapped_column(String(255))
    protocol: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(16))
    effective_host: Mapped[str | None] = mapped_column(String(255))
    effective_port: Mapped[int | None] = mapped_column(Integer)
    auth_sources: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    client_ip: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(server_default=utcnow_sql())
    first_connected_at: Mapped[datetime | None]
    ended_at: Mapped[datetime | None]
    end_reason: Mapped[str | None] = mapped_column(String(64))
    attach_count: Mapped[int] = mapped_column(Integer, default=0)


# --------------------------------------------------------------------------- settings & misc


class UserPreference(Base):
    __tablename__ = "user_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(server_default=utcnow_sql(), onupdate=utcnow_sql())


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(server_default=utcnow_sql(), onupdate=utcnow_sql())
    updated_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class ImportJob(Base):
    __tablename__ = "import_jobs"
    __table_args__ = (
        CheckConstraint(_in("status", ("previewed", "applied", "failed", "expired")), name="status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    source_format: Mapped[str] = mapped_column(String(32))
    filename: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default="previewed")
    created_at: Mapped[datetime] = mapped_column(server_default=utcnow_sql())
    applied_at: Mapped[datetime | None]
    preview: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (
        UniqueConstraint("user_id", "key", name="user_key"),
        Index("ix_idempotency_created", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(255))
    method: Mapped[str] = mapped_column(String(8))
    path: Mapped[str] = mapped_column(String(512))
    request_hash: Mapped[str] = mapped_column(String(64))
    status_code: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[Any | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(server_default=utcnow_sql())
