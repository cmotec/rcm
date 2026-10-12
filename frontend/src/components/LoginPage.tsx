import { LogIn, MonitorSmartphone } from 'lucide-react';
import { useEffect, useState } from 'react';
import { ApiError, post } from '../api/client';
import type { AuthConfig } from '../api/types';

const ERRORS: Record<string, string> = {
  idp_error: 'The identity provider reported an error.',
  state_missing: 'The login session expired. Please try again.',
  state_mismatch: 'The login request could not be verified. Please try again.',
  token_invalid: 'The identity provider’s response could not be validated.',
  not_provisioned: 'Your account is not provisioned in this application.',
  inactive: 'Your account is disabled.',
  no_role: 'Your account has no role in this application. Ask an administrator to map your group.',
};

const AUTO_REDIRECT_KEY = 'sso-auto-redirect-at';
/** Within this time after an automatic redirect, coming back to the login page shows it instead (no loop). */
const AUTO_REDIRECT_GUARD_MS = 30_000;

function recentlyAutoRedirected(): boolean {
  try {
    return Date.now() - Number(sessionStorage.getItem(AUTO_REDIRECT_KEY) ?? 0) < AUTO_REDIRECT_GUARD_MS;
  } catch {
    return false;
  }
}

function markAutoRedirect(): void {
  try {
    sessionStorage.setItem(AUTO_REDIRECT_KEY, String(Date.now()));
  } catch {
    /* storage unavailable: the guard just does not apply */
  }
}

export function LoginPage({ config, onLoggedIn }: { config: AuthConfig; onLoggedIn: () => void }) {
  const params = new URLSearchParams(window.location.search);
  const urlError = params.get('error');
  const signedOut = params.has('signed_out');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(urlError ? ERRORS[urlError] ?? urlError : null);
  const [busy, setBusy] = useState(false);
  const returnTo = window.location.pathname.startsWith('/login') ? '/' : window.location.pathname + window.location.search;
  const ssoHref = `/api/v1/auth/oidc/login?return_to=${encodeURIComponent(returnTo)}`;
  // Only SSO is offered: go to the identity provider right away, unless the user just signed out,
  // the IdP reported an error, or we were just there (avoids a redirect loop).
  const [autoRedirect] = useState(
    () => config.oidc_enabled && !config.local_enabled && !urlError && !signedOut && !recentlyAutoRedirected(),
  );
  useEffect(() => {
    if (!autoRedirect) return;
    markAutoRedirect();
    window.location.replace(ssoHref);
  }, [autoRedirect, ssoHref]);

  const local = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await post('/api/v1/auth/local/login', { username, password });
      window.history.replaceState(null, '', '/');
      onLoggedIn();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login">
      <div className="login-card">
        <div className="login-brand"><MonitorSmartphone size={28} /><h1>{config.app_display_name}</h1></div>
        {error && <div className="banner error">{error}</div>}
        {signedOut && !error && <div className="banner info">You have been signed out.</div>}
        {autoRedirect && <p className="muted">Redirecting to {config.oidc_display_name}…</p>}
        {config.oidc_enabled && (
          <a className="button primary block" href={ssoHref}>
            <LogIn size={16} /> Sign in with {config.oidc_display_name}
          </a>
        )}
        {config.oidc_enabled && config.local_enabled && <div className="divider-text">or recovery login</div>}
        {config.local_enabled && (
          <form onSubmit={local}>
            <label className="field"><span>Username</span><input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" autoFocus={!config.oidc_enabled} /></label>
            <label className="field"><span>Password</span><input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" /></label>
            <button className={`block ${config.oidc_enabled ? '' : 'primary'}`} disabled={busy || !username || !password}>Sign in</button>
          </form>
        )}
      </div>
    </div>
  );
}
