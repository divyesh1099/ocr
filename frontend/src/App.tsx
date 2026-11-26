import { useCallback, useEffect, useState } from 'react';
import { Authenticator } from '@aws-amplify/ui-react';
import { NavLink, Navigate, Route, Routes } from 'react-router-dom';
import { api, logout, usesCloudflareAccess, type PublicSettings, type User } from './api';
import Dashboard from './pages/Dashboard';
import Home from './pages/Home';
import Compress from './pages/Compress';
import NewJob from './pages/NewJob';
import JobDetail from './pages/JobDetail';
import Billing from './pages/Billing';
import Admin from './pages/Admin';

export interface Session {
  user: User;
  settings: PublicSettings;
  refresh: () => Promise<void>;
}

function Landing({ settings }: { settings: PublicSettings | null }) {
  const mode = settings?.accessMode;
  return (
    <div className="landing">
      <h1>Read it. Shrink it.</h1>
      <p className="lede">
        Pull the text out of scanned PDFs and photos, or make any file smaller: PDFs, photos, videos, audio and
        Office documents. Single files, zip archives or whole folders.
      </p>
      <ul className="points">
        <li><b>OCR.</b> Text as TXT, JSON with line positions, or a searchable PDF. English and 11 Indian languages.</li>
        <li><b>Compress.</b> Pick how small. Nothing ever comes back bigger than it went in.</li>
        <li><b>Any size.</b> Multi-gigabyte files and folders upload straight to storage, structure kept.</li>
      </ul>
      <p className="muted small">
        {mode === 'invite' && 'Accounts are invite only right now.'}
        {mode === 'approval' && 'New accounts are reviewed before they can upload.'}
        {mode === 'open' && settings?.billingEnabled && 'Sign up, buy page credits and start right away.'}
        {mode === 'open' && !settings?.billingEnabled && 'Sign up and start right away.'}
      </p>
    </div>
  );
}

function Shell({ session, children }: { session: Session; children: React.ReactNode }) {
  const { user, settings } = session;
  return (
    <div className="shell">
      <header className="topbar">
        <NavLink to="/" className="brand"><span className="logo" />{settings.siteName}</NavLink>
        <nav>
          <NavLink to="/" end>Home</NavLink>
          <NavLink to="/ocr">OCR</NavLink>
          {settings.compressEnabled && <NavLink to="/compress">Compress</NavLink>}
          <NavLink to="/jobs" end>Jobs</NavLink>
          {settings.billingEnabled && <NavLink to="/billing">Credits</NavLink>}
          {user.role === 'superadmin' && <NavLink to="/admin">Admin</NavLink>}
        </nav>
        <div className="who">
          {settings.billingEnabled && !user.unlimited && <span className="credits">{user.credits.toLocaleString()} credits</span>}
          <span className="email">{user.email}</span>
          <button className="ghost" onClick={() => logout()}>Sign out</button>
        </div>
      </header>
      <main>{children}</main>
    </div>
  );
}

function SignedIn() {
  const [session, setSession] = useState<Session | null>(null);
  const [error, setError] = useState('');
  const refresh = useCallback(async () => {
    try {
      const me = await api<{ user: User; settings: PublicSettings }>('/me');
      setSession({ user: me.user, settings: me.settings, refresh });
    } catch (e: any) {
      setError(e.message);
    }
  }, []);
  useEffect(() => { refresh(); }, [refresh]);

  if (error) {
    return (
      <div className="center card narrow">
        <p>Could not load your account: {error}</p>
        <button onClick={() => logout()}>Sign out</button>
      </div>
    );
  }
  if (!session) return <div className="center muted">Loading…</div>;
  if (session.user.status !== 'active') {
    return (
      <div className="center card narrow">
        <h2>{session.user.status === 'pending' ? 'Waiting for approval' : 'Account suspended'}</h2>
        <p>
          {session.user.status === 'pending'
            ? 'Your account was created. The administrator has to approve it before you can upload files. Check back soon.'
            : 'This account has been suspended. Contact the administrator if you think this is a mistake.'}
        </p>
        <button onClick={() => logout()}>Sign out</button>
      </div>
    );
  }
  return (
    <Shell session={session}>
      <Routes>
        <Route path="/" element={<Home session={session} />} />
        <Route path="/ocr" element={<NewJob session={session} />} />
        <Route path="/new" element={<Navigate to="/ocr" replace />} />
        <Route path="/compress" element={<Compress session={session} />} />
        <Route path="/jobs" element={<Dashboard session={session} />} />
        <Route path="/jobs/:jobId" element={<JobDetail session={session} />} />
        <Route path="/billing" element={<Billing session={session} />} />
        <Route path="/admin" element={session.user.role === 'superadmin' ? <Admin session={session} /> : <Navigate to="/" />} />
        <Route path="*" element={<Navigate to="/" />} />
      </Routes>
    </Shell>
  );
}

export default function App() {
  const [settings, setSettings] = useState<PublicSettings | null>(null);
  useEffect(() => {
    api<PublicSettings>('/public/config', { auth: false }).then(setSettings).catch(() => undefined);
  }, []);
  const inviteOnly = settings?.accessMode === "invite";
  // Behind Cloudflare Access the visitor is already signed in before the page loads.
  if (usesCloudflareAccess()) return <SignedIn />;
  return (
    <Authenticator.Provider>
      <Gate settings={settings} inviteOnly={inviteOnly} />
    </Authenticator.Provider>
  );
}

function Gate({ settings, inviteOnly }: { settings: PublicSettings | null; inviteOnly: boolean }) {
  return (
    <Authenticator
      loginMechanisms={['email']}
      signUpAttributes={['email']}
      components={{
        Header: () => <Landing settings={settings} />,
        SignUp: inviteOnly
          ? { Footer: () => <p className="muted small pad">Only invited email addresses can create an account.</p> }
          : undefined,
      }}
    >
      {() => <SignedIn />}
    </Authenticator>
  );
}
