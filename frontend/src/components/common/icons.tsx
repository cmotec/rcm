import {
  Box,
  Briefcase,
  Building,
  Cloud,
  Cpu,
  Database,
  Folder,
  FolderOpen,
  FolderSync,
  Globe,
  HardDrive,
  House,
  Laptop,
  Lock,
  Monitor,
  Network,
  ScreenShare,
  Server,
  Shield,
  Star,
  Tag,
  Terminal,
  type LucideIcon,
} from 'lucide-react';
import type { Protocol } from '../../api/types';

export const PROTOCOL_ICONS: Record<Protocol, LucideIcon> = {
  host: Server,
  ssh: Terminal,
  sftp: FolderSync,
  rdp: Monitor,
  vnc: ScreenShare,
  ftp: HardDrive,
  ftps: HardDrive,
};

export const PROTOCOL_LABELS: Record<Protocol, string> = {
  host: 'Host (group of connections)',
  ssh: 'SSH',
  sftp: 'SFTP',
  rdp: 'RDP',
  vnc: 'VNC',
  ftp: 'FTP (unencrypted)',
  ftps: 'FTPS',
};

/** Icons selectable for folders (stored by name). */
export const FOLDER_ICONS: Record<string, LucideIcon> = {
  folder: Folder,
  server: Server,
  database: Database,
  cloud: Cloud,
  network: Network,
  building: Building,
  briefcase: Briefcase,
  house: House,
  laptop: Laptop,
  cpu: Cpu,
  globe: Globe,
  shield: Shield,
  lock: Lock,
  box: Box,
  tag: Tag,
  star: Star,
};

export const FOLDER_COLORS = ['#64748b', '#2563eb', '#0891b2', '#059669', '#65a30d', '#ca8a04', '#ea580c', '#dc2626',
  '#db2777', '#9333ea', '#4f46e5'];

export function ProtocolIcon({ protocol, size = 15 }: { protocol: Protocol; size?: number }) {
  const Icon = PROTOCOL_ICONS[protocol] ?? Server;
  return <Icon size={size} className={`proto-icon proto-${protocol}`} aria-hidden />;
}

export function FolderGlyph({ icon, color, open, size = 15 }: { icon: string | null; color: string | null; open?: boolean;
  size?: number }) {
  const Icon = icon && FOLDER_ICONS[icon] ? FOLDER_ICONS[icon] : open ? FolderOpen : Folder;
  return <Icon size={size} style={{ color: color ?? undefined }} className="folder-icon" aria-hidden />;
}
