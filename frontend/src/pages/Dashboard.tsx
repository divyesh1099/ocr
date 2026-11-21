import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api, bytes, type Job, when } from '../api';
import type { Session } from '../App';

export function StatusBadge({ job }: { job: Job }) {
  const label =
    job.status === 'done' && job.docsFailed ? 'done with errors'
      : job.status === 'created' ? 'not submitted'
        : job.status;
  return <span className={`badge s-${job.status}${job.docsFailed ? ' warn' : ''}`}>{label}</span>;
}

export default function Dashboard({ session }: { session: Session }) {
  const [jobs, setJobs] = useState<Job[] | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [error, setError] = useState('');

  const load = async (more = false) => {
    try {
      const res = await api<{ jobs: Job[]; cursor: string | null }>(`/jobs${more && cursor ? `?cursor=${cursor}` : ''}`);
      setJobs((prev) => (more && prev ? [...prev, ...res.jobs] : res.jobs));
      setCursor(res.cursor);
    } catch (e: any) {
      setError(e.message);
    }
  };
  useEffect(() => {
    load();
    const t = setInterval(() => load(), 10000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <section>
      <div className="row between">
        <h1>Your jobs</h1>
        <Link className="button" to="/new">New job</Link>
      </div>
      {error && <p className="error">{error}</p>}
      {jobs === null ? <p className="muted">Loading…</p> : jobs.length === 0 ? (
        <div className="card empty">
          <p>No jobs yet. Start one by uploading PDFs, images, zip files or a whole folder.</p>
          {session.settings.billingEnabled && !session.user.unlimited && (
            <p className="muted">You have {session.user.credits.toLocaleString()} credits.</p>
          )}
        </div>
      ) : (
        <table className="table">
          <thead>
            <tr><th>Name</th><th>Status</th><th>Files</th><th>Pages</th><th>Engine</th><th>Created</th></tr>
          </thead>
          <tbody>
            {jobs.map((j) => (
              <tr key={j.jobId}>
                <td><Link to={`/jobs/${j.jobId}`}>{j.name}</Link></td>
                <td><StatusBadge job={j} /></td>
                <td>{j.docsDone + j.docsFailed + j.docsSkipped}/{j.docsTotal}</td>
                <td>{j.pagesDone.toLocaleString()}{j.pagesTotal ? ` / ${j.pagesTotal.toLocaleString()}` : ''}</td>
                <td>{j.engine}</td>
                <td className="muted">{when(j.createdAt)}{j.resultBytes ? ` · ${bytes(j.resultBytes)}` : ''}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {cursor && <button className="ghost" onClick={() => load(true)}>Load more</button>}
    </section>
  );
}
