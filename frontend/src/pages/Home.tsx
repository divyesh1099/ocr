import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api, isCompress, saving, type Job, when } from '../api';
import type { Session } from '../App';
import { StatusBadge } from './Dashboard';

/** Shown right after sign-in: pick what to do, then recent jobs. */
export default function Home({ session }: { session: Session }) {
  const [recent, setRecent] = useState<Job[] | null>(null);
  useEffect(() => {
    api<{ jobs: Job[] }>('/jobs?limit=5').then((r) => setRecent(r.jobs)).catch(() => setRecent([]));
  }, []);
  const first = session.user.email.split('@')[0];

  return (
    <section className="home">
      <h1>Hi {first}, what would you like to do?</h1>
      <div className="modes">
        <Link to="/ocr" className="mode">
          <svg className="mode-icon" viewBox="0 0 44 44" aria-hidden><rect width="44" height="44" rx="10" />
            <path d="M12 14h20M12 22h13M12 30h17" /></svg>
          <b>OCR</b>
          <span>Get the text out of scanned PDFs, photos and images. Download it as text, JSON or a searchable PDF.</span>
        </Link>
        <Link to="/compress" className={`mode${session.settings.compressEnabled ? '' : ' off'}`}>
          <svg className="mode-icon" viewBox="0 0 44 44" aria-hidden><rect width="44" height="44" rx="10" />
            <path d="M22 7v10m-5-5 5 5 5-5M22 37V27m-5 5 5-5 5 5M12 22h20" /></svg>
          <b>Compress</b>
          <span>Make PDFs, photos, videos, audio and Office files smaller. Choose how small and download them all at once.</span>
        </Link>
      </div>

      {recent && recent.length > 0 && (
        <>
          <div className="row between">
            <h2>Recent jobs</h2>
            <Link to="/jobs">All jobs</Link>
          </div>
          <table className="table">
            <tbody>
              {recent.map((j) => (
                <tr key={j.jobId}>
                  <td><Link to={`/jobs/${j.jobId}`}>{j.name}</Link></td>
                  <td>{isCompress(j) ? 'Compress' : 'OCR'}</td>
                  <td><StatusBadge job={j} /></td>
                  <td className="muted">{isCompress(j) ? saving(j.bytesIn, j.bytesOut) : `${j.pagesDone.toLocaleString()} pages`}</td>
                  <td className="muted">{when(j.createdAt)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </section>
  );
}
