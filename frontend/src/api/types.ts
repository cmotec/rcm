// Types mirroring the REST API (see /api/docs for the authoritative OpenAPI document).

export type Protocol = 'host' | 'ssh' | 'sftp' | 'rdp' | 'vnc' | 'ftp' | 'ftps';
export type AuthMode = 'inherit' | 'direct' | 'credential' | 'interactive';
export type PermissionLevel = 'view' | 'connect' | 'edit' | 'manage';

export interface Problem {
  type: string;
  title: string;
  status: number;
  code: string;
  detail?: string;
  request_id?: string;
  [extra: string]: unknown;
}

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface AuthConfig {
  oidc_enabled: boolean;
  oidc_display_name: string;
  local_enabled: boolean;
}

export interface Me {
  id: string;
  username: string;
  email: string | null;
  display_name: string | null;
  roles: string[];
  permissions: string[];
  is_admin: boolean;
  csrf_token: string | null;
  auth_method: string;
  session_expires_at: string | null;
  /** Signage account: only this connection, fullscreen, no menus (null = none assigned yet). */
  signage?: boolean;
  signage_connection_id?: string | null;
}

export interface Folder {
  id: string;
  name: string;
  parent_id: string | null;
  icon: string | null;
  color: string | null;
  description: string | null;
  search_domain: string | null;
  sort_order: number;
  owner_id: string | null;
  version: number;
  created_at: string;
  updated_at: string;
  permission: PermissionLevel | null;
}

export interface SecurityWarning {
  code: string;
  message: string;
}

export interface Connection {
  id: string;
  name: string;
  protocol: Protocol;
  hostname: string | null;
  port: number | null;
  folder_id: string | null;
  parent_id: string | null;
  auth_mode: AuthMode;
  inherit_auth: boolean;
  username: string | null;
  domain: string | null;
  password_set: boolean;
  credential_id: string | null;
  search_domain: string | null;
  description: string | null;
  tags: string[];
  settings: Record<string, unknown>;
  sort_order: number;
  owner_id: string | null;
  version: number;
  created_at: string;
  updated_at: string;
  permission: PermissionLevel | null;
  favorite: boolean;
  warnings: SecurityWarning[];
  /** field -> {{lookup:<credential name pattern>:<field>}} */
  lookups: Partial<Record<LookupField, string>>;
  /** "choose a credential at connect time": only credentials from this credential folder */
  credential_folder_id: string | null;
  /** display values of lookup fields, resolved on the server (hostname, port, description, …) */
  resolved: Partial<Record<LookupField, string>>;
}

export type LookupField = 'username' | 'password' | 'domain' | 'private_key' | 'passphrase'
  | 'hostname' | 'port' | 'search_domain' | 'description';

export interface Tree {
  folders: Folder[];
  connections: Connection[];
}

export interface ValueSource {
  kind: 'connection' | 'credential' | 'interactive' | 'folder' | 'environment' | 'default' | 'lookup';
  id: string | null;
  name: string | null;
}

export interface ResolvedValue<T = unknown> {
  value?: T;
  set?: boolean;
  source: ValueSource;
  inherited: boolean;
}

export interface EffectiveConnection {
  connection_id: string;
  protocol: Protocol;
  launchable: boolean;
  hostname: ResolvedValue<string> | null;
  port: ResolvedValue<number> | null;
  search_domains: ResolvedValue<string[]> | null;
  hostname_candidates: string[];
  auth_mode: AuthMode;
  auth: Partial<Record<'username' | 'password' | 'domain' | 'private_key' | 'passphrase', ResolvedValue<string>>>;
  prompt_required: boolean;
  credential_choice_required: boolean;
  credential_choice_folder_id?: string | null;
  interactive_connection_id: string | null;
  chain: { id: string; name: string; protocol: Protocol }[];
  description?: ResolvedValue<string> | null;
  /** field -> why its lookup failed */
  lookup_errors?: Record<string, string>;
}

export interface Credential {
  id: string;
  name: string;
  parent_id: string | null;
  username: string | null;
  domain: string | null;
  password_set: boolean;
  private_key_set: boolean;
  passphrase_set: boolean;
  private_key_fingerprint: string | null;
  description: string | null;
  metadata: Record<string, unknown>;
  owner_id: string | null;
  rotation_interval_days: number | null;
  secret_changed_at: string | null;
  rotation_due: boolean;
  version: number;
  created_at: string;
  updated_at: string;
  permission: PermissionLevel | null;
  external_fields: string[];
  usage_count: number;
  folder_id: string | null;
  /** non-secret values for lookups (port, hostname, description, …) */
  attributes: Record<string, string>;
}

export interface CredentialFolder {
  id: string;
  name: string;
  parent_id: string | null;
  description: string | null;
  owner_id: string | null;
  version: number;
  permission: PermissionLevel | null;
}

export interface CredentialSecrets {
  username: string | null;
  domain: string | null;
  password: string | null;
  private_key: string | null;
  passphrase: string | null;
}

export type SessionKind = 'guac' | 'terminal' | 'files';

export interface SessionInfo {
  id: string;
  connection_id: string | null;
  connection_name: string;
  protocol: Protocol;
  kind: SessionKind;
  state: string;
  created_at: string;
  connected_at: string | null;
  last_activity_at: string;
  effective_host: string | null;
  effective_port: number | null;
  host_resolution_note: string | null;
  warnings: SecurityWarning[];
  auth_sources: Record<string, ValueSource>;
  owner_id: string;
  owner_name: string | null;
  error: string | null;
  ticket: string | null;
  ticket_expires_at: string | null;
  tunnel_path: string | null;
  client_settings: Record<string, unknown>;
}

export interface HostKeyInfo {
  hostname: string;
  port: number;
  key_type: string;
  fingerprint: string;
  public_key: string;
  connection_name: string;
  previous_fingerprint?: string;
  can_replace?: boolean;
}

export interface FileEntry {
  name: string;
  path: string;
  type: 'file' | 'dir' | 'link' | 'other';
  size: number | null;
  modified: string | null;
  permissions: string | null;
  mode: number | null;
  owner: string | null;
  group: string | null;
  link_target: string | null;
}

export interface AclEntry {
  id: string;
  resource_type: string;
  resource_id: string;
  principal_type: 'user' | 'group' | 'role';
  principal_id: string;
  principal_name: string | null;
  permission: PermissionLevel;
}

export interface Role {
  id: string;
  name: string;
  description: string | null;
  builtin: boolean;
  permissions: string[];
  resource_cap: PermissionLevel;
}

export interface User {
  id: string;
  username: string;
  email: string | null;
  display_name: string | null;
  is_local: boolean;
  is_active: boolean;
  last_login_at: string | null;
  created_at: string;
  roles: string[];
  groups: string[];
  is_signage?: boolean;
  signage_connection_id?: string | null;
}

export interface AuditEvent {
  id: number;
  ts: string;
  actor_id: string | null;
  actor_name: string | null;
  action: string;
  resource_type: string | null;
  resource_id: string | null;
  outcome: 'success' | 'failure' | 'denied';
  ip: string | null;
  request_id: string | null;
  details: Record<string, unknown>;
}

export interface KnownHost {
  id: string;
  hostname: string;
  port: number;
  key_type: string;
  fingerprint: string;
  public_key: string;
  created_at: string;
  last_seen_at: string | null;
}

export interface SecretProvider {
  id: string;
  name: string;
  type: 'openbao';
  config: Record<string, unknown>;
  secret_fields_set: string[];
  enabled: boolean;
  managed_by_env: boolean;
  sync_interval_seconds: number;
  last_sync_at: string | null;
  last_sync_status: string;
  last_sync_error: string | null;
  mapping_count: number;
  conflict_count: number;
}

export interface SecretMapping {
  id: string;
  credential_id: string;
  provider_id: string;
  field: string;
  external_ref: { path: string; key: string; environment?: string };
  conflict_policy: 'manual' | 'prefer_remote' | 'prefer_local';
  delete_policy: 'keep' | 'clear';
  status: string;
  message: string | null;
  last_synced_at: string | null;
}

export interface ApiToken {
  id: string;
  name: string;
  prefix: string;
  read_only: boolean;
  expires_at: string | null;
  last_used_at: string | null;
  created_at: string;
  revoked_at: string | null;
  token?: string;
}
