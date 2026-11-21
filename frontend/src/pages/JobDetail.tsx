import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { api, bytes, type Doc, type Job, when } from '../api';
import type { Session } from '../App';
import { StatusBadge } from './Dashboard';

export default function JobDetail({ session }: { session: Session }) {
  const { jobId } = useParams();
  const nav = useNavigate();
  const [job, setJob] = useState<Job | null>(null);
  const [docs, setDocs] = useState<Doc[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [filter, setFilter] = useState('');
  const [error, setError] = useState('');

  const loadJob = async () => {
    try {
      const { job } = await api<{ job: Job }>(`/jobs/${jobId}`);
      setJob(job);
      return job;
    } catch (e: any) {
      setError(e.message);
    }
  };
  const loadDocs = async (more = false) => {
    const q = new URLSearchParams();
    if (more && cursor) q.set('cursor', cursor);
    if (filter) q.set('status', filter);
    const res = await api<{ documents: Doc[]; cursor: string | null }>(`/jobs/${jobId}/documents?${q}`);
    setDocs((prev) => (more ? [...prev, ...res.documents] : res.documents));
    setCursor(res.cursor);
  };

  useEffect(() => {
    let stop = false;
    const tick = async () => {
      const j = await loadJob();
      await loadDocs().catch(() => undefined);
      if (!stop && j && (j.status === 'processing' || j.status === 'finalizing')) setTimeout(tick, 4000);
      if (j?.status === 'done') session.refresh();
    };
    tick();
    return () => { stop = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId, filter]);

  const download = async (path: string) => {
    const { url } = await api<{ url: string }>(path);
    window.location.href = url;
  };
  const remove = async () => {
    if (!confirm('Delete this job and all its files and results? This cannot be undone.')) return;
    await api(`/jobs/${jobId}`, { method: 'DELETE' });
    nav('/');
  };
  const forceFinish = async () => {
    if (!confirm('Finish this job now with whatever is done so far?')) return;
    await api(`/admin/jobs/${jobId}/finalize`, { method: 'POST' });
    loadJob();
  };

  if (error) return <p className="error">{error}</p>;
  if (!job) return <p className="muted">Loading…</p>;
  const finished = job.docsDone + job.docsFailed + job.docsSkipped;
  const pct = job.pagesTotal ? (job.pagesDone / job.pagesTotal) * 100 : job.docsTotal ? (finished / job.docsTotal) * 100 : 0;

  return (
    <section>
      <div className="row between">
        <div>
          <h1>{job.name}</h1>
          <p className="muted small">
            {job.engine}{job.language && job.language !== 'eng' ? ` · ${job.language}` : ''} · created {when(job.createdAt)}
            {job.finishedAt && ` · finished ${when(job.finishedAt)}`}
          </p>
        </div>
        <div className="row gap">
          {job.hasResult && <button onClick={() => download(`/jobs/${jobId}/download`)}>Download all ({bytes(job.resultBytes)})</button>}
          {session.user.role === 'superadmin' && job.status === 'processing' && <button className="ghost" onClick={forceFinish}>Force finish</button>}
          {job.status !== 'processing' && job.status !== 'finalizing' && <button className="ghost danger" onClick={remove}>Delete</button>}
        </div>
      </div>

      <div className="stats card">
        <div><StatusBadge job={job} /></div>
        <div><b>{finished.toLocaleString()}</b> / {job.docsTotal.toLocaleString()} files</div>
        <div><b>{job.pagesDone.toLocaleString()}</b> / {job.pagesTotal.toLocaleString()} pages</div>
        <div>{job.docsFailed > 0 && <span className="error">{job.docsFailed} failed</span>} {job.docsSkipped > 0 && <span className="muted">{job.docsSkipped} skipped</span>}</div>
        {session.settings.billingEnabled && <div className="muted">{job.creditsCharged.toLocaleString()} credits used</div>}
      </div>
      {(job.status === 'processing' || job.status === 'finalizing') && (
        <div className="bar big"><span style={{ width: `${Math.min(99, pct)}%` }} /></div>
      )}
      {job.status === 'finalizing' && <p className="muted">Packing results into a zip…</p>}
      {job.lastError && <p className="error small">{job.lastError}</p>}

      <div className="row between">
        <h2>Files</h2>
        <select value={filter} onChange={(e) => setFilter(e.target.value)}>
          <option value="">All</option><option value="done">Done</option><option value="failed">Failed</option>
          <option value="skipped">Skipped</option><option value="processing">Processing</option>
        </select>
      </div>
      <table className="table">
        <thead><tr><th>File</th><th>Status</th><th>Pages</th><th>Size</th><th>Download</th></tr></thead>
        <tbody>
          {docs.map((d) => (
            <tr key={d.docId}>
              <td className="path">{d.relPath}{d.error && <div className="error small">{d.error}</div>}</td>
              <td><span className={`badge d-${d.status}`}>{d.status}{d.status === 'processing' && d.unitsTotal > 1 ? ` ${d.unitsDone}/${d.unitsTotal}` : ''}</span></td>
              <td>{d.pages || ''}</td>
              <td className="muted">{bytes(d.size)}</td>
              <td className="row gap">
                {Object.keys(d.outputs || {}).sort().map((fmt) => (
                  <button key={fmt} className="ghost small" onClick={() => download(`/jobs/${jobId}/documents/${d.docId}/download?format=${fmt}`)}>{fmt}</button>
                ))}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {cursor && <button className="ghost" onClick={() => loadDocs(true)}>Load more</button>}
    </section>
  );
}
