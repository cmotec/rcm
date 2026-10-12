/** Product mark (same as the favicon). The product name itself is configurable (APP_DISPLAY_NAME). */
export function Logo({ size = 26 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden="true" className="logo">
      <rect width="32" height="32" rx="7" fill="#2563eb" />
      <rect x="6" y="8" width="20" height="13" rx="2" fill="none" stroke="#fff" strokeWidth="2" />
      <path d="M12 25h8M16 21v4" stroke="#fff" strokeWidth="2" strokeLinecap="round" />
    </svg>
  );
}
