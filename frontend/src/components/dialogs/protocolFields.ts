import type { Protocol } from '../../api/types';

export interface FieldSpec {
  key: string;
  label: string;
  type: 'bool' | 'select' | 'number' | 'text' | 'textarea' | 'list' | 'ssh-connection' | 'tunnel';
  options?: { value: string; label: string }[];
  help?: string;
  placeholder?: string;
  group: string;
  defaultValue?: unknown;
  showIf?: (s: Record<string, unknown>, protocol: Protocol) => boolean;
}

const hostKeyPolicy: FieldSpec = {
  key: 'host_key_policy', label: 'Host key verification', type: 'select', group: 'Security',
  options: [
    { value: '', label: 'Server default' },
    { value: 'prompt', label: 'Ask on first connection (trust on first use)' },
    { value: 'strict', label: 'Strict — only keys added by an administrator' },
    { value: 'accept-new', label: 'Accept new keys automatically (not recommended)' },
  ],
};
const connectTimeout: FieldSpec = { key: 'connect_timeout', label: 'Connect timeout (s)', type: 'number', group: 'Connection', placeholder: 'server default' };
const jumpHost: FieldSpec = { key: 'jump_host_connection_id', label: 'Jump host', type: 'ssh-connection', group: 'Connection',
  help: 'Connect through another SSH connection (ProxyJump).' };
const tunnel: FieldSpec = { key: 'ssh_tunnel', label: 'SSH tunnel', type: 'tunnel', group: 'Security',
  help: 'Route the connection through an SSH connection (encrypts VNC/RDP traffic up to the SSH server).' };
const autoReconnect: FieldSpec = { key: 'auto_reconnect', label: 'Reconnect automatically after interruptions', type: 'bool', group: 'Connection', defaultValue: true };

export const PROTOCOL_FIELDS: Record<Protocol, FieldSpec[]> = {
  host: [],
  ssh: [
    { key: 'engine', label: 'Engine', type: 'select', group: 'Connection', defaultValue: 'native',
      options: [{ value: 'native', label: 'Native (xterm.js, jump hosts, host-key prompts)' }, { value: 'guacd', label: 'guacd (server-side rendering)' }] },
    connectTimeout,
    { key: 'keepalive_interval', label: 'Keep-alive interval (s)', type: 'number', group: 'Connection', placeholder: 'server default' },
    jumpHost,
    autoReconnect,
    hostKeyPolicy,
    { key: 'pinned_host_key', label: 'Pinned host key (guacd engine)', type: 'textarea', group: 'Security',
      showIf: (s) => s.engine === 'guacd', help: 'known_hosts line, used only when no trusted key is stored.' },
    { key: 'term_type', label: 'Terminal type', type: 'text', group: 'Terminal', defaultValue: 'xterm-256color' },
    { key: 'command', label: 'Command instead of shell', type: 'text', group: 'Terminal', placeholder: 'login shell' },
    { key: 'font_size', label: 'Font size', type: 'number', group: 'Terminal', placeholder: '14' },
    { key: 'color_scheme', label: 'Colour scheme', type: 'select', group: 'Terminal', options: [
      { value: '', label: 'Default (dark)' }, { value: 'light', label: 'Light' }, { value: 'green-black', label: 'Green on black' },
      { value: 'white-black', label: 'White on black' }, { value: 'black-white', label: 'Black on white' }] },
    { key: 'scrollback', label: 'Scrollback lines', type: 'number', group: 'Terminal', placeholder: '5000' },
  ],
  sftp: [
    connectTimeout,
    jumpHost,
    hostKeyPolicy,
    { key: 'start_directory', label: 'Start directory', type: 'text', group: 'Files', placeholder: 'home directory' },
    { key: 'show_hidden', label: 'Show hidden files', type: 'bool', group: 'Files' },
  ],
  rdp: [
    { key: 'security', label: 'Security mode', type: 'select', group: 'Security', defaultValue: 'any', options: [
      { value: 'any', label: 'Negotiate' }, { value: 'nla', label: 'NLA (Network Level Authentication)' },
      { value: 'nla-ext', label: 'Extended NLA' }, { value: 'tls', label: 'TLS' }, { value: 'rdp', label: 'Standard RDP encryption' },
      { value: 'vmconnect', label: 'Hyper-V / VMConnect' }] },
    { key: 'cert_policy', label: 'Certificate validation', type: 'select', group: 'Security', options: [
      { value: '', label: 'Server default' }, { value: 'validate', label: 'Validate (trusted CA required)' },
      { value: 'fingerprints', label: 'Accept only the pinned fingerprints' },
      { value: 'tofu', label: 'Accept automatically on first use' }, { value: 'ignore', label: 'Do not validate (insecure)' }] },
    { key: 'cert_fingerprints', label: 'Pinned certificate fingerprints', type: 'list', group: 'Security',
      showIf: (s) => s.cert_policy === 'fingerprints', placeholder: 'sha256:AB:CD:…', help: 'One per line.' },
    tunnel,
    { key: 'resize_method', label: 'When the window size changes', type: 'select', group: 'Display', defaultValue: 'display-update', options: [
      { value: 'display-update', label: 'Update remote resolution (RDP display update)' },
      { value: 'reconnect', label: 'Reconnect with the new resolution' },
      { value: 'none', label: 'Keep resolution, scale the picture' }] },
    { key: 'width', label: 'Fixed width', type: 'number', group: 'Display', placeholder: 'window size' },
    { key: 'height', label: 'Fixed height', type: 'number', group: 'Display', placeholder: 'window size' },
    { key: 'dpi', label: 'DPI', type: 'number', group: 'Display', placeholder: 'automatic (high-DPI aware)' },
    { key: 'color_depth', label: 'Colour depth', type: 'select', group: 'Display', options: [
      { value: '', label: 'Automatic' }, { value: '8', label: '8 bit' }, { value: '16', label: '16 bit' },
      { value: '24', label: '24 bit' }, { value: '32', label: '32 bit' }] },
    { key: 'enable_wallpaper', label: 'Show wallpaper', type: 'bool', group: 'Display' },
    { key: 'enable_theming', label: 'Enable theming', type: 'bool', group: 'Display' },
    { key: 'enable_font_smoothing', label: 'Font smoothing (ClearType)', type: 'bool', group: 'Display', defaultValue: true },
    { key: 'enable_full_window_drag', label: 'Full window drag', type: 'bool', group: 'Display' },
    { key: 'enable_desktop_composition', label: 'Desktop composition', type: 'bool', group: 'Display' },
    { key: 'enable_menu_animations', label: 'Menu animations', type: 'bool', group: 'Display' },
    { key: 'disable_copy', label: 'Block copying from the remote desktop', type: 'bool', group: 'Clipboard & devices' },
    { key: 'disable_paste', label: 'Block pasting into the remote desktop', type: 'bool', group: 'Clipboard & devices' },
    { key: 'normalize_clipboard', label: 'Line endings in clipboard', type: 'select', group: 'Clipboard & devices', options: [
      { value: '', label: 'Unchanged' }, { value: 'preserve', label: 'Preserve' }, { value: 'unix', label: 'Unix (LF)' }, { value: 'windows', label: 'Windows (CRLF)' }] },
    { key: 'enable_drive', label: 'Drive redirection (file transfer)', type: 'bool', group: 'Clipboard & devices' },
    { key: 'drive_name', label: 'Drive name', type: 'text', group: 'Clipboard & devices', placeholder: 'Transfer', showIf: (s) => Boolean(s.enable_drive) },
    { key: 'disable_upload', label: 'Block uploads to the drive', type: 'bool', group: 'Clipboard & devices', showIf: (s) => Boolean(s.enable_drive) },
    { key: 'disable_download', label: 'Block downloads from the drive', type: 'bool', group: 'Clipboard & devices', showIf: (s) => Boolean(s.enable_drive) },
    { key: 'disable_audio', label: 'Disable audio playback', type: 'bool', group: 'Clipboard & devices' },
    { key: 'enable_audio_input', label: 'Microphone', type: 'bool', group: 'Clipboard & devices' },
    { key: 'enable_printing', label: 'Printer redirection (PDF)', type: 'bool', group: 'Clipboard & devices' },
    { key: 'console', label: 'Connect to console / admin session', type: 'bool', group: 'Session' },
    { key: 'server_layout', label: 'Keyboard layout', type: 'select', group: 'Session', options: [
      { value: '', label: 'Automatic (en-us)' }, { value: 'en-us-qwerty', label: 'English (US)' }, { value: 'en-gb-qwerty', label: 'English (UK)' },
      { value: 'de-de-qwertz', label: 'German' }, { value: 'de-ch-qwertz', label: 'Swiss German' }, { value: 'fr-fr-azerty', label: 'French' },
      { value: 'fr-ch-qwertz', label: 'Swiss French' }, { value: 'it-it-qwerty', label: 'Italian' }, { value: 'es-es-qwerty', label: 'Spanish' },
      { value: 'sv-se-qwerty', label: 'Swedish' }, { value: 'ja-jp-qwerty', label: 'Japanese' }, { value: 'failsafe', label: 'Unicode fail-safe' }] },
    { key: 'timezone', label: 'Time zone', type: 'text', group: 'Session', placeholder: 'browser time zone' },
    { key: 'initial_program', label: 'Initial program', type: 'text', group: 'Session' },
    { key: 'remote_app', label: 'RemoteApp program', type: 'text', group: 'Session', placeholder: '||notepad' },
    { key: 'remote_app_dir', label: 'RemoteApp working directory', type: 'text', group: 'Session', showIf: (s) => Boolean(s.remote_app) },
    { key: 'remote_app_args', label: 'RemoteApp arguments', type: 'text', group: 'Session', showIf: (s) => Boolean(s.remote_app) },
    { key: 'client_name', label: 'Client name', type: 'text', group: 'Session' },
    { key: 'read_only', label: 'View only (no input)', type: 'bool', group: 'Session' },
    autoReconnect,
    { key: 'gateway_hostname', label: 'RD Gateway host', type: 'text', group: 'Gateway & load balancing' },
    { key: 'gateway_port', label: 'RD Gateway port', type: 'number', group: 'Gateway & load balancing', placeholder: '443' },
    { key: 'gateway_domain', label: 'RD Gateway domain', type: 'text', group: 'Gateway & load balancing' },
    { key: 'load_balance_info', label: 'Load balance info', type: 'text', group: 'Gateway & load balancing' },
    { key: 'preconnection_id', label: 'Preconnection ID', type: 'number', group: 'Gateway & load balancing' },
    { key: 'preconnection_blob', label: 'Preconnection BLOB', type: 'text', group: 'Gateway & load balancing' },
  ],
  vnc: [
    { key: 'display', label: 'Display number', type: 'number', group: 'Connection', defaultValue: 0, help: 'Default port = 5900 + display.' },
    tunnel,
    autoReconnect,
    { key: 'color_depth', label: 'Colour depth', type: 'select', group: 'Display', options: [
      { value: '', label: 'Automatic' }, { value: '8', label: '8 bit' }, { value: '16', label: '16 bit' }, { value: '24', label: '24 bit' }, { value: '32', label: '32 bit' }] },
    { key: 'cursor', label: 'Cursor', type: 'select', group: 'Display', options: [
      { value: '', label: 'Default' }, { value: 'local', label: 'Local' }, { value: 'remote', label: 'Remote' }] },
    { key: 'encodings', label: 'Encodings', type: 'text', group: 'Display', placeholder: 'e.g. zrle ultra copyrect hextile zlib corre rre raw' },
    { key: 'swap_red_blue', label: 'Swap red/blue', type: 'bool', group: 'Display' },
    { key: 'read_only', label: 'View only (no input)', type: 'bool', group: 'Session' },
    { key: 'disable_copy', label: 'Block copying from the remote desktop', type: 'bool', group: 'Clipboard' },
    { key: 'disable_paste', label: 'Block pasting into the remote desktop', type: 'bool', group: 'Clipboard' },
    { key: 'clipboard_encoding', label: 'Clipboard encoding', type: 'select', group: 'Clipboard', options: [
      { value: '', label: 'ISO 8859-1 (standard)' }, { value: 'UTF-8', label: 'UTF-8' }, { value: 'UTF-16', label: 'UTF-16' }, { value: 'CP1252', label: 'CP1252' }] },
    { key: 'dest_host', label: 'Repeater destination host', type: 'text', group: 'Repeater' },
    { key: 'dest_port', label: 'Repeater destination port', type: 'number', group: 'Repeater' },
  ],
  ftp: [],
  ftps: [],
};

const ftpFields: FieldSpec[] = [
  { key: 'mode', label: 'Data connection mode', type: 'select', group: 'Connection', defaultValue: 'passive', options: [
    { value: 'passive', label: 'Passive (recommended)' }, { value: 'active', label: 'Active (server connects back)' }] },
  { key: 'trust_pasv_address', label: 'Use the address announced in PASV replies', type: 'bool', group: 'Connection',
    help: 'Off: data connections go to the control host (works behind NAT).' },
  { key: 'tls_mode', label: 'TLS mode', type: 'select', group: 'Security', defaultValue: 'explicit',
    showIf: (_s, p) => p === 'ftps', options: [
      { value: 'explicit', label: 'Explicit (AUTH TLS, usually port 21)' }, { value: 'implicit', label: 'Implicit (usually port 990)' }] },
  { key: 'tls_verify', label: 'Certificate validation', type: 'select', group: 'Security', showIf: (_s, p) => p === 'ftps', options: [
    { value: '', label: 'Server default' }, { value: 'true', label: 'Validate' }, { value: 'false', label: 'Do not validate (insecure)' }] },
  { key: 'transfer_type', label: 'Transfer type', type: 'select', group: 'Files', defaultValue: 'binary', options: [
    { value: 'binary', label: 'Binary' }, { value: 'ascii', label: 'ASCII (convert line endings)' }] },
  { key: 'encoding', label: 'File name encoding', type: 'text', group: 'Files', defaultValue: 'utf-8' },
  { key: 'timeout', label: 'Timeout (s)', type: 'number', group: 'Connection', placeholder: 'server default' },
  { key: 'start_directory', label: 'Start directory', type: 'text', group: 'Files', placeholder: 'login directory' },
  { key: 'show_hidden', label: 'Show hidden files', type: 'bool', group: 'Files' },
];
PROTOCOL_FIELDS.ftp = ftpFields;
PROTOCOL_FIELDS.ftps = ftpFields;

export function defaultPort(protocol: Protocol, settings: Record<string, unknown>): number | null {
  switch (protocol) {
    case 'ssh':
    case 'sftp':
      return 22;
    case 'rdp':
      return 3389;
    case 'vnc':
      return 5900 + Number(settings.display ?? 0);
    case 'ftp':
      return 21;
    case 'ftps':
      return settings.tls_mode === 'implicit' ? 990 : 21;
    default:
      return null;
  }
}

/** Convert form values to API settings (drop empty values, coerce numbers/booleans). */
export function cleanSettings(protocol: Protocol, values: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const f of PROTOCOL_FIELDS[protocol]) {
    const v = values[f.key];
    if (f.showIf && !f.showIf(values, protocol)) continue;
    if (v === undefined || v === null || v === '') continue;
    if (f.type === 'number') {
      const n = Number(v);
      if (!Number.isNaN(n)) out[f.key] = n;
    } else if (f.key === 'color_depth') {
      out[f.key] = Number(v);
    } else if (f.key === 'tls_verify') {
      out[f.key] = v === 'true' || v === true;
    } else if (f.type === 'list') {
      const list = (Array.isArray(v) ? v : String(v).split('\n')).map((x) => String(x).trim()).filter(Boolean);
      if (list.length) out[f.key] = list;
    } else if (f.type === 'tunnel') {
      const id = typeof v === 'object' ? (v as { via_connection_id?: string }).via_connection_id : v;
      if (id) out[f.key] = { via_connection_id: id };
    } else {
      out[f.key] = v;
    }
  }
  return out;
}
