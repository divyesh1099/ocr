import { useEffect, useState } from 'react';
import { api, money, type User, when } from '../api';
import type { Session } from '../App';

function SettingsPanel({ session }: { session: Session }) {
  const [s, setS] = useState<any>(null);
  const [rzp, setRzp] = useState(false);
  const [packsText, setPacksText] = useState('');
  const [msg, setMsg] = useState('');
  const [error, setError] = useState('');

  useEffect(() => {
    api<any>('/admin/settings').then((r) => {
      setS(r.settings);
      setRzp(r.razorpayConfigured);
      setPacksText(JSON.stringify(r.settings.packs, null, 2));
    });
  }, []);
  if (!s) return <p className="muted">Loading settings…</p>;

  const save = async () => {
    setMsg('');
    setError('');
    try {
      const packs = JSON.parse(packsText);
      const r = await api<any>('/admin/settings', { method: 'PUT', body: { ...s, packs } });
      setS(r.settings);
      setMsg('Saved.');
      session.refresh();
    } catch (e: any) {
      setError(e.message);
    }
  };
  const set = (k: string, v: any) => setS({ ...s, [k]: v });
  const toggleEngine = (e: string) =>
    set('enabledEngines', s.enabledEngines.includes(e) ? s.enabledEngines.filter((x: string) => x !== e) : [...s.enabledEngines, e]);

  return (
    <div className="card form">
      <h2>Access and billing</h2>
      <fieldset>
        <legend>Who can sign up?</legend>
        <label className="check"><input type="radio" checked={s.accessMode === 'invite'} onChange={() => set('accessMode', 'invite')} /> <b>Invite only</b>: only emails you invite below</label>
        <label className="check"><input type="radio" checked={s.accessMode === 'approval'} onChange={() => set('accessMode', 'approval')} /> <b>Approval</b>: anyone can sign up, you approve each account</label>
        <label className="check"><input type="radio" checked={s.accessMode === 'open'} onChange={() => set('accessMode', 'open')} /> <b>Open</b>: anyone can sign up and use it (and buy credits)</label>
      </fieldset>
      <label className="check"><input type="checkbox" checked={s.billingEnabled} onChange={(e) => set('billingEnabled', e.target.checked)} /> Charge credits for OCR (enables Razorpay purchases)</label>
      {s.billingEnabled && !rzp && <p className="error small">Razorpay keys are not set. Put them in the RazorpaySecret in AWS Secrets Manager (see README).</p>}
      <div className="grid3">
        <label>Free credits on sign-up<input type="number" min={0} value={s.freeCreditsOnSignup} onChange={(e) => set('freeCreditsOnSignup', Number(e.target.value))} /></label>
        <label>Textract credits / page<input type="number" min={0} value={s.prices.textract} onChange={(e) => set('prices', { ...s.prices, textract: Number(e.target.value) })} /></label>
        <label>Tesseract credits / page<input type="number" min={0} value={s.prices.tesseract} onChange={(e) => set('prices', { ...s.prices, tesseract: Number(e.target.value) })} /></label>
      </div>
      <fieldset>
        <legend>Engines</legend>
        {['textract', 'tesseract'].map((e) => (
          <label key={e} className="check"><input type="checkbox" checked={s.enabledEngines.includes(e)} onChange={() => toggleEngine(e)} /> {e}</label>
        ))}
        <label>Default engine
          <select value={s.defaultEngine} onChange={(e) => set('defaultEngine', e.target.value)}>
            {s.enabledEngines.map((e: string) => <option key={e}>{e}</option>)}
          </select>
        </label>
      </fieldset>
      <div className="grid3">
        <label>Site name<input value={s.siteName} onChange={(e) => set('siteName', e.target.value)} /></label>
        <label>Max file size (GB)<input type="number" min={1} value={Math.round(s.maxFileBytes / 1024 ** 3)} onChange={(e) => set('maxFileBytes', Number(e.target.value) * 1024 ** 3)} /></label>
        <label>Currency<input value={s.currency} onChange={(e) => set('currency', e.target.value.toUpperCase())} /></label>
      </div>
      <label>Credit packs (JSON; amount is in paise, so 19900 = {money(19900, s.currency)})
        <textarea rows={8} value={packsText} onChange={(e) => setPacksText(e.target.value)} spellCheck={false} />
      </label>
      {error && <p className="error">{error}</p>}
      {msg && <p className="ok">{msg}</p>}
      <button onClick={save}>Save settings</button>
    </div>
  );
}

function Users() {
  const [users, setUsers] = useState<User[]>([]);
  const [status, setStatus] = useState('');
  const [error, setError] = useState('');
  const load = () => api<{ users: User[] }>(`/admin/users${status ? `?status=${status}` : ''}`).then((r) => setUsers(r.users)).catch((e) => setError(e.message));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { load(); }, [status]);

  const patch = async (u: User, body: any) => {
    try {
      await api(`/admin/users/${u.sub}`, { method: 'PATCH', body });
      load();
    } catch (e: any) {
      setError(e.message);
    }
  };
  const credits = (u: User) => {
    const v = prompt(`Add credits for ${u.email} (use a negative number to remove)`, '1000');
    if (v && Number(v)) patch(u, { creditsDelta: Number(v), reason: 'admin grant' });
  };

  return (
    <div className="card">
      <div className="row between">
        <h2>Users</h2>
        <select value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">All</option><option value="pending">Waiting for approval</option><option value="active">Active</option><option value="banned">Suspended</option>
        </select>
      </div>
      {error && <p className="error">{error}</p>}
      <table className="table">
        <thead><tr><th>Email</th><th>Status</th><th>Credits</th><th>Joined</th><th></th></tr></thead>
        <tbody>
          {users.map((u) => (
            <tr key={u.sub}>
              <td>{u.email}{u.role === 'superadmin' && <span className="badge">superadmin</span>}</td>
              <td><span className={`badge u-${u.status}`}>{u.status}</span></td>
              <td>{u.unlimited ? 'unlimited' : u.credits.toLocaleString()}</td>
              <td className="muted">{when(u.createdAt)}</td>
              <td className="row gap">
                {u.status !== 'active' && <button className="small" onClick={() => patch(u, { status: 'active' })}>Approve</button>}
                {u.status === 'active' && u.role !== 'superadmin' && <button className="ghost small danger" onClick={() => patch(u, { status: 'banned' })}>Suspend</button>}
                <button className="ghost small" onClick={() => credits(u)}>Credits</button>
                {u.role !== 'superadmin' && <button className="ghost small" onClick={() => patch(u, { unlimited: !u.unlimited })}>{u.unlimited ? 'Limit' : 'Unlimited'}</button>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Invites() {
  const [invites, setInvites] = useState<any[]>([]);
  const [emails, setEmails] = useState('');
  const [credits, setCredits] = useState(100);
  const load = () => api<{ invites: any[] }>('/admin/invites').then((r) => setInvites(r.invites));
  useEffect(() => { load(); }, []);
  const add = async () => {
    const list = emails.split(/[\s,;]+/).filter((e) => e.includes('@'));
    if (!list.length) return;
    await api('/admin/invites', { body: { emails: list, credits } });
    setEmails('');
    load();
  };
  return (
    <div className="card form">
      <h2>Invitations</h2>
      <p className="muted small">In invite-only mode, only these email addresses can create an account. They start with the credits you set here.</p>
      <textarea rows={3} placeholder="one@example.com, two@example.com" value={emails} onChange={(e) => setEmails(e.target.value)} />
      <div className="row gap">
        <label className="inline">Credits <input type="number" min={0} value={credits} onChange={(e) => setCredits(Number(e.target.value))} /></label>
        <button onClick={add}>Invite</button>
      </div>
      <table className="table">
        <thead><tr><th>Email</th><th>Credits</th><th>Invited</th><th>Used</th><th></th></tr></thead>
        <tbody>
          {invites.map((i) => (
            <tr key={i.email}>
              <td>{i.email}</td><td>{i.credits}</td><td className="muted">{when(i.createdAt)}</td><td className="muted">{i.usedAt ? when(i.usedAt) : '-'}</td>
              <td><button className="ghost small" onClick={async () => { await api(`/admin/invites/${encodeURIComponent(i.email)}`, { method: 'DELETE' }); load(); }}>Remove</button></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function Admin({ session }: { session: Session }) {
  return (
    <section className="admin">
      <h1>Admin</h1>
      <SettingsPanel session={session} />
      <Users />
      <Invites />
    </section>
  );
}
