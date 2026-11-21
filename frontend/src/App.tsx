import { useCallback, useEffect, useState } from 'react';
import { Authenticator } from '@aws-amplify/ui-react';
import { signOut } from 'aws-amplify/auth';
import { NavLink, Navigate, Route, Routes } from 'react-router-dom';
import { api, type PublicSettings, type User } from './api';
import Dashboard from './pages/Dashboard';
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
      <h1>Text out of anything.</h1>
      <p className="lede">
        PDFs with thousands of pages, scanned images, zip archives and whole folders. Upload them and download the
        text as TXT, JSON with line positions, or searchable PDF.
      </p>
      <ul className="points">
        <li><b>Any size.</b> Multi-gigabyte files upload straight to storage in parallel parts.</li>
        <li><b>Zips and folders.</b> Nested archives are unpacked and folder structure is kept in the results.</li>
        <li><b>Two engines.</b> AWS Textract for the best accuracy, Tesseract for bulk work and Indian languages.</li>
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
          <NavLink to="/" end>Jobs</NavLink>
          <NavLink to="/new">New job</NavLink>
          {settings.billingEnabled && <NavLink to="/billing">Credits</NavLink>}
          {user.role === 'superadmin' && <NavLink to="/admin">Admin</NavLink>}
        </nav>
        <div className="who">
          {settings.billingEnabled && !user.unlimited && <span className="credits">{user.credits.toLocaleString()} credits</span>}
          <span className="email">{user.email}</span>
          <button className="ghost" onClick={() => signOut()}>Sign out</button>
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

  if (error) return <div className="center card">Could not load your account: {error}</div>;
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
        <button onClick={() => signOut()}>Sign out</button>
      </div>
    );
  }
  return (
    <Shell session={session}>
      <Routes>
        <Route path="/" element={<Dashboard session={session} />} />
        <Route path="/new" element={<NewJob session={session} />} />
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
