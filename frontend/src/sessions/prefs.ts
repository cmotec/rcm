/** User preferences that session controllers read while running (kept in sync by the registry). */
export const sessionPrefs = {
  /** macOS: Command acts as Ctrl on remote desktops (⌘C/⌘V copy and paste in Windows). */
  macCommandAsCtrl: true,
};

export function applySessionPrefs(prefs: Record<string, unknown>): void {
  sessionPrefs.macCommandAsCtrl = prefs.mac_command_as_ctrl !== false;
}
