import { QueryClient, QueryClientProvider, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  KeyRound,
  LayoutDashboard,
  LogOut,
  Menu,
  PanelLeft,
  Moon,
  PanelRight,
  PlugZap,
  Settings,
  Sun,
  Ticket,
  UserRound,
} from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, get, post, setCsrfToken, setUnauthenticatedHandler } from './api/client';
import { hasPerm, keys, useMe, useTree } from './api/hooks';
import type { AuthConfig, Connection, Folder } from './api/types';
import { AdminView } from './components/admin/AdminView';
import { DialogHost } from './components/common/DialogHost';
import { CredentialsView } from './components/credentials/CredentialsView';
import { DetailsPanel } from './components/DetailsPanel';
import { AclDialog } from './components/dialogs/AclDialog';
import { ConnectionDialog } from './components/dialogs/ConnectionDialog';
import { FolderDialog } from './components/dialogs/FolderDialog';
import { LoginPage } from './components/LoginPage';
import { SignageView } from './components/SignageView';
import { ProfileDialog } from './components/ProfileDialog';
import { TokensDialog } from './components/TokensDialog';
import { ConnectionTree } from './components/tree/ConnectionTree';
import {
  disconnectAll,
  loadPreferences,
  restoreWorkspace,
  savedWorkspace,
  startLayoutPersistence,
} from './sessions/registry';
import { dialogs } from './state/dialogs';
import { useTreePrefs } from './state/treePrefs';
import { useWorkspace } from './state/workspace';
import { savedConnectionIds } from './workspace/model';
import { Workspace } from './workspace/Workspace';
import { Logo } from './components/common/Logo';
import { MOBILE_QUERY, useMediaQuery } from './util/useMediaQuery';

const queryClient = new QueryClient({
  defaultOptions: { queries: { refetchOnWindowFocus: false, retry: (n, err) => !(err instanceof ApiError && err.status < 500) && n < 2 } },
});

type View = 'workspace' | 'credentials' | 'admin';

function useTheme(): [string, () => void] {
  const [theme, setTheme] = useState(() => localStorage.getItem('theme') ?? 'auto');
  useEffect(() => {
    document.documentElement.dataset.theme = theme === 'auto' ? '' : theme;
    localStorage.setItem('theme', theme);
  }, [theme]);
  return [theme, () => setTheme((t) => (t === 'dark' ? 'light' : 'dark'))];
}

function Shell({ config }: { config: AuthConfig }) {
  const { data: me } = useMe();
  const { data: tree } = useTree();
  const qc = useQueryClient();
  const [view, setView] = useState<View>('workspace');
  const [theme, toggleTheme] = useTheme();
  const [selected, setSelected] = useState<Connection | null>(null);
  const [showDetails, setShowDetails] = useState(true);
  const [connDialog, setConnDialog] = useState<{ connection?: Connection; placement?: { folderId?: string | null; parentId?: string | null } } | null>(null);
  const [folderDialog, setFolderDialog] = useState<{ folder?: Folder; parentId?: string | null } | null>(null);
  const [acl, setAcl] = useState<{ type: 'folder' | 'connection'; id: string; name: string } | null>(null);
  const [tokens, setTokens] = useState(false);
  const [profile, setProfile] = useState(false);
  const [sidebarWidth, setSidebarWidth] = useState(() => Number(localStorage.getItem('sidebar.width') ?? 300));
  const [restoreOffer, setRestoreOffer] = useState<number>(0);
  const isMobile = useMediaQuery(MOBILE_QUERY);
  // Left menu: hidden while sessions are open (fullscreen workspace), toggled from the toolbar.
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const sessionCount = useWorkspace((s) => Object.keys(s.sessions).length);
  const banner = useQuery({ queryKey: ['app-settings'], queryFn: () => get<{ values: { banner_message?: string | null } }>('/api/v1/settings') });
  const started = useRef(false);

  useEffect(() => {
    document.title = config.app_display_name;
  }, [config.app_display_name]);

  useEffect(() => {
    if (me?.csrf_token) setCsrfToken(me.csrf_token);
  }, [me]);

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    void loadPreferences().then((prefs) => {
      useTreePrefs.getState().hydrate(prefs.tree);
      const saved = savedWorkspace();
      const n = saved ? savedConnectionIds(saved).length : 0;
      if (n) setRestoreOffer(n);
    });
  }, []);

  // Subscribe/unsubscribe symmetrically (StrictMode runs effects twice in development).
  useEffect(() => startLayoutPersistence(), []);

  // When a session opens, the details panel hides (all screen sizes) and, on phones only, the left
  // menu too (fullscreen session). Both return once all sessions are closed. On desktop the left
  // menu stays as the user set it. The toolbar toggles still work in between.
  const prevSessionCount = useRef(sessionCount);
  useEffect(() => {
    if (sessionCount > prevSessionCount.current) {
      setShowDetails(false);
      if (isMobile) setSidebarOpen(false);
    } else if (sessionCount === 0 && prevSessionCount.current > 0) {
      setShowDetails(true);
      if (isMobile) setSidebarOpen(true);
    }
    prevSessionCount.current = sessionCount;
  }, [sessionCount, isMobile]);

  const goToWorkspace = () => {
    setView('workspace');
    if (isMobile && sessionCount === 0) setSidebarOpen(true);
  };

  // keep the selected connection fresh after tree reloads
  useEffect(() => {
    if (selected && tree) setSelected(tree.connections.find((c) => c.id === selected.id) ?? null);
  }, [tree]); // eslint-disable-line react-hooks/exhaustive-deps

  const logout = async () => {
    if (sessionCount && !(await dialogs.confirm({ title: 'Sign out', message: 'Sign out? Open remote sessions keep running on the server until they time out; close them first if you want them ended.', confirmLabel: 'Sign out' }))) return;
    const r = await post<{ redirect: string | null }>('/api/v1/auth/logout').catch(() => ({ redirect: null }));
    // signed_out: the login page must not send an SSO-only user straight back to the IdP
    window.location.href = r.redirect ?? '/login?signed_out=1';
  };

  const restore = async () => {
    const saved = savedWorkspace();
    setRestoreOffer(0);
    if (!saved || !tree) return;
    const names = new Map(tree.connections.map((c) => [c.id, c]));
    const n = await restoreWorkspace(saved, names);
    dialogs.toast(`Restored ${n} session(s)`, 'success');
  };

  const onSidebarResize = (e: React.PointerEvent) => {
    const start = e.clientX;
    const initial = sidebarWidth;
    const target = e.currentTarget as HTMLElement;
    target.setPointerCapture(e.pointerId);
    const move = (ev: PointerEvent) => setSidebarWidth(Math.max(200, Math.min(640, initial + ev.clientX - start)));
    const up = () => {
      target.removeEventListener('pointermove', move);
      target.removeEventListener('pointerup', up);
      setSidebarWidth((w) => {
        localStorage.setItem('sidebar.width', String(w));
        return w;
      });
    };
    target.addEventListener('pointermove', move);
    target.addEventListener('pointerup', up);
  };

  const isAdminish = ['users.manage', 'roles.manage', 'secrets.manage', 'settings.manage', 'import.run', 'known_hosts.manage'].some((p) => hasPerm(me, p));

  return (
    <div className="app">
      <header className="toolbar">
        {view === 'workspace' && (
          <button className="icon-btn" title={sidebarOpen ? 'Hide connections' : 'Show connections'}
                  aria-expanded={sidebarOpen} onClick={() => setSidebarOpen((v) => !v)}>
            {isMobile ? <Menu size={18} /> : <PanelLeft size={16} />}
          </button>
        )}
        <button className="brand" onClick={goToWorkspace} title="Workspace">
          <Logo />
          <span className="brand-name">{config.app_display_name}</span>
        </button>
        <nav className="views">
          <button className={view === 'workspace' ? 'active' : ''} onClick={goToWorkspace} title="Workspace">
            <LayoutDashboard size={15} /><span className="label">Workspace</span>
            {sessionCount > 0 && <span className="count">{sessionCount}</span>}</button>
          <button className={view === 'credentials' ? 'active' : ''} onClick={() => setView('credentials')} title="Credentials">
            <KeyRound size={15} /><span className="label">Credentials</span></button>
          {(isAdminish || hasPerm(me, 'api.access')) && (
            <button className={view === 'admin' ? 'active' : ''} onClick={() => setView('admin')} title={isAdminish ? 'Administration' : 'Activity'}>
              <Settings size={15} /><span className="label">{isAdminish ? 'Administration' : 'Activity'}</span></button>
          )}
        </nav>
        <span className="spacer" />
        {view === 'workspace' && (
          <>
            {!isMobile && (
              <button className="icon-btn" title={showDetails ? 'Hide details panel' : 'Show details panel'} onClick={() => setShowDetails((v) => !v)}><PanelRight size={16} /></button>
            )}
            <button className="icon-btn" title="Disconnect all sessions" disabled={!sessionCount} onClick={() => void disconnectAll()}><PlugZap size={16} /></button>
          </>
        )}
        <button className="icon-btn" title="Toggle theme" onClick={toggleTheme}>{theme === 'dark' ? <Sun size={16} /> : <Moon size={16} />}</button>
        <button className="icon-btn" title="API tokens" onClick={() => setTokens(true)}><Ticket size={16} /></button>
        <button className="user" title={`Profile: ${me?.display_name ?? me?.username ?? ''} (${me?.roles.join(', ') ?? ''})`}
                onClick={() => setProfile(true)}>
          <UserRound size={15} /><span className="label">{me?.display_name ?? me?.username}</span></button>
        <button className="icon-btn" title="Sign out" onClick={() => void logout()}><LogOut size={16} /></button>
      </header>
      {banner.data?.values.banner_message && <div className="banner info global">{banner.data.values.banner_message}</div>}
      {restoreOffer > 0 && view === 'workspace' && (
        <div className="banner info global">
          Reopen the {restoreOffer} session(s) and window layout from your last visit?
          <button className="primary" onClick={() => void restore()}>Restore</button>
          <button onClick={() => setRestoreOffer(0)}>Dismiss</button>
        </div>
      )}
      <div className="main" style={{ display: view === 'workspace' ? undefined : 'none' }}>
        {isMobile && sidebarOpen && <div className="drawer-backdrop" onClick={() => setSidebarOpen(false)} />}
        <aside className={`sidebar ${isMobile ? 'drawer' : ''} ${sidebarOpen ? '' : 'collapsed'}`}
               style={isMobile ? undefined : { width: sidebarWidth }}>
          <ConnectionTree tapToOpen={isMobile} actions={{
            editConnection: (c) => setConnDialog({ connection: c }),
            newConnection: (placement) => setConnDialog({ placement }),
            newFolder: (parentId) => setFolderDialog({ parentId }),
            editFolder: (folder) => setFolderDialog({ folder }),
            permissions: (type, id, name) => setAcl({ type, id, name }),
            select: setSelected,
            selectedId: selected?.id ?? null,
          }} />
        </aside>
        {!isMobile && sidebarOpen && (
          <div className="sidebar-resizer" onPointerDown={onSidebarResize} role="separator" aria-orientation="vertical" />
        )}
        <Workspace emptyHint={isMobile ? <>
          <p><b>No open sessions.</b></p>
          <p className="muted">Open the menu and tap a connection.</p>
          <button className="primary" onClick={() => setSidebarOpen(true)}><Menu size={14} /> Connections</button>
        </> : <>
          <p><b>No open sessions.</b></p>
          <p className="muted">Double-click a connection, or drag it here. Open several sessions at once: each opens in a new tab —
            drag a tab into the workspace to float it, or onto the edge of a session to split the view.</p>
        </>} />
        {!isMobile && showDetails && selected && (
          <DetailsPanel connection={selected} onClose={() => setSelected(null)} onEdit={() => setConnDialog({ connection: selected })} />
        )}
      </div>
      {view === 'credentials' && <CredentialsView />}
      {view === 'admin' && <AdminView />}
      {connDialog && <ConnectionDialog {...connDialog} onClose={() => { setConnDialog(null); void qc.invalidateQueries({ queryKey: keys.tree }); }} />}
      {folderDialog && <FolderDialog {...folderDialog} onClose={() => setFolderDialog(null)} />}
      {acl && <AclDialog resourceType={acl.type} resourceId={acl.id} name={acl.name} onClose={() => setAcl(null)} />}
      {tokens && <TokensDialog onClose={() => setTokens(false)} />}
      {profile && <ProfileDialog me={me} onClose={() => setProfile(false)} />}
    </div>
  );
}

function AuthGate() {
  const config = useQuery({ queryKey: ['auth-config'], queryFn: () => get<AuthConfig>('/api/v1/auth/config') });
  const me = useMe();
  const qc = useQueryClient();
  const onLoggedIn = useCallback(() => void qc.invalidateQueries({ queryKey: keys.me }), [qc]);

  useEffect(() => {
    setUnauthenticatedHandler(() => void qc.invalidateQueries({ queryKey: keys.me }));
  }, [qc]);

  if (config.isLoading || me.isLoading) return <div className="splash">Loading…</div>;
  if (config.error) return <div className="splash error">Cannot reach the server: {String(config.error)}</div>;
  const unauth = me.error instanceof ApiError && (me.error.status === 401 || me.error.status === 403);
  if (unauth || !me.data) {
    if (me.error instanceof ApiError && me.error.code === 'no_role') {
      return <div className="splash error">Your account has no role in this application. Ask an administrator for access.</div>;
    }
    if (!window.location.pathname.startsWith('/login')) window.history.replaceState(null, '', `/login${window.location.search}`);
    return <LoginPage config={config.data!} onLoggedIn={onLoggedIn} />;
  }
  if (window.location.pathname.startsWith('/login')) window.history.replaceState(null, '', '/');
  if (me.data.signage) return <SignageView />;
  return <Shell config={config.data!} />;
}

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <AuthGate />
      <DialogHost />
    </QueryClientProvider>
  );
}
