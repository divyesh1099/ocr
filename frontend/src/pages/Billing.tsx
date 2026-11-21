import { useEffect, useState } from 'react';
import { api, money, when } from '../api';
import type { Session } from '../App';

declare global {
  interface Window { Razorpay?: any }
}

function loadRazorpay(): Promise<void> {
  if (window.Razorpay) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = 'https://checkout.razorpay.com/v1/checkout.js';
    s.onload = () => resolve();
    s.onerror = () => reject(new Error('Could not load Razorpay checkout'));
    document.body.appendChild(s);
  });
}

export default function Billing({ session }: { session: Session }) {
  const { settings, user } = session;
  const [ledger, setLedger] = useState<any[]>([]);
  const [busy, setBusy] = useState('');
  const [msg, setMsg] = useState('');
  const [error, setError] = useState('');

  const loadLedger = () => api<{ entries: any[] }>('/billing/ledger').then((r) => setLedger(r.entries)).catch(() => undefined);
  useEffect(() => { loadLedger(); }, []);

  const buy = async (packId: string) => {
    setBusy(packId);
    setError('');
    setMsg('');
    try {
      await loadRazorpay();
      const order = await api<any>('/billing/orders', { body: { packId } });
      await new Promise<void>((resolve, reject) => {
        const rzp = new window.Razorpay({
          key: order.keyId,
          order_id: order.orderId,
          amount: order.amount,
          currency: order.currency,
          name: settings.siteName,
          description: `${order.pack.credits.toLocaleString()} OCR credits`,
          prefill: { email: order.email },
          theme: { color: '#1f3a5f' },
          handler: async (resp: any) => {
            try {
              await api('/billing/verify', { body: resp });
              resolve();
            } catch (e) { reject(e); }
          },
          modal: { ondismiss: () => reject(new Error('Payment cancelled')) },
        });
        rzp.on('payment.failed', (r: any) => reject(new Error(r.error?.description || 'Payment failed')));
        rzp.open();
      });
      setMsg('Payment received. Your credits have been added.');
      await session.refresh();
      loadLedger();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy('');
    }
  };

  return (
    <section>
      <h1>Credits</h1>
      <p>
        {user.unlimited ? 'Your account has unlimited usage.' : <>You have <b>{user.credits.toLocaleString()}</b> credits.</>}{' '}
        <span className="muted">Textract costs {settings.prices.textract} and Tesseract {settings.prices.tesseract} credit(s) per page.</span>
      </p>
      {msg && <p className="ok">{msg}</p>}
      {error && <p className="error">{error}</p>}
      {!settings.razorpayKeyId && <p className="muted">Online payments are not set up yet. Ask the administrator for credits.</p>}
      <div className="packs">
        {settings.packs.map((p) => (
          <div className="card pack" key={p.id}>
            <h3>{p.name}</h3>
            <p className="big">{p.credits.toLocaleString()} <span className="muted small">credits</span></p>
            <p>{money(p.amount, settings.currency)}</p>
            <p className="muted small">≈ {Math.floor(p.credits / Math.max(1, settings.prices.textract)).toLocaleString()} Textract pages</p>
            <button disabled={!settings.razorpayKeyId || !!busy} onClick={() => buy(p.id)}>{busy === p.id ? 'Opening…' : 'Buy'}</button>
          </div>
        ))}
      </div>
      <h2>History</h2>
      <table className="table">
        <thead><tr><th>When</th><th>Change</th><th>Reason</th></tr></thead>
        <tbody>
          {ledger.map((e, i) => (
            <tr key={i}><td className="muted">{when(e.at)}</td><td className={e.delta < 0 ? 'error' : 'ok'}>{e.delta > 0 ? '+' : ''}{e.delta.toLocaleString()}</td><td>{e.reason}</td></tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
