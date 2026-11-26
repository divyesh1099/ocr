import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, type CompressOptions, type Job } from '../api';
import type { Session } from '../App';
import FilePicker, { UploadProgress } from '../components/FilePicker';
import { type PickedFile, type Progress, uploadAll } from '../upload';

const LEVELS: { value: CompressOptions['level']; title: string; text: string }[] = [
  { value: 'strong', title: 'Smallest file', text: 'Biggest savings. Photos and scans lose some detail.' },
  { value: 'balanced', title: 'Balanced', text: 'Much smaller, still looks the same on screen. Recommended.' },
  { value: 'light', title: 'Best quality', text: 'Gentle. Good for printing; saves less.' },
];

export default function Compress({ session }: { session: Session }) {
  const { settings } = session;
  const nav = useNavigate();
  const [files, setFiles] = useState<PickedFile[]>([]);
  const [name, setName] = useState('');
  const [opts, setOpts] = useState<CompressOptions>({
    level: 'balanced', imageFormat: 'keep', maxImageSide: 0, videoHeight: 0, audioBitrate: 0,
  });
  const [progress, setProgress] = useState<Progress | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const set = <K extends keyof CompressOptions>(k: K, v: CompressOptions[K]) => setOpts((o) => ({ ...o, [k]: v }));

  const add = (good: PickedFile[]) => {
    setFiles((prev) => [...prev, ...good]);
    if (!name && good.length) setName(good.length === 1 ? good[0].path.split('/').pop()! : good[0].path.split('/')[0]);
  };

  const start = async () => {
    setBusy(true);
    setError('');
    try {
      const { job } = await api<{ job: Job }>('/jobs', { body: { mode: 'compress', name, compress: opts } });
      const result = await uploadAll(job.jobId, files, setProgress);
      if (result.errors.length && result.errors.length === files.length) {
        throw new Error(`All uploads failed. First error: ${result.errors[0]}`);
      }
      await api(`/jobs/${job.jobId}/submit`, { method: 'POST' });
      nav(`/jobs/${job.jobId}`);
    } catch (e: any) {
      setError(e.message);
      setBusy(false);
    }
  };

  if (!settings.compressEnabled) {
    return <section className="narrow-wide"><h1>Compress files</h1><p className="muted">Compression isn't available on this server.</p></section>;
  }

  return (
    <section className="narrow-wide">
      <h1>Compress files</h1>
      <p className="muted">PDFs, photos, videos, audio, Word/Excel/PowerPoint files and more. Zips are opened and everything inside is compressed.</p>

      <FilePicker files={files} onAdd={add} onClear={() => setFiles([])} busy={busy} maxFileBytes={settings.maxFileBytes}
        hint="Any file type. Folders keep their structure in the download." />

      <div className="card form">
        <label>Job name<input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Wedding photos" /></label>

        <fieldset>
          <legend>How small?</legend>
          <div className="choices">
            {LEVELS.map((l) => (
              <label key={l.value} className={`choice${opts.level === l.value ? ' on' : ''}`}>
                <input type="radio" name="level" checked={opts.level === l.value} onChange={() => set('level', l.value)} />
                <b>{l.title}</b>
                <span className="muted small">{l.text}</span>
              </label>
            ))}
          </div>
        </fieldset>

        <details className="advanced">
          <summary>More options</summary>
          <div className="grid3">
            <label>Photo format
              <select value={opts.imageFormat} onChange={(e) => set('imageFormat', e.target.value as CompressOptions['imageFormat'])}>
                <option value="keep">Keep the same format</option>
                <option value="webp">Convert to WebP (smallest)</option>
                <option value="jpeg">Convert to JPEG</option>
              </select>
            </label>
            <label>Photo size
              <select value={opts.maxImageSide} onChange={(e) => set('maxImageSide', Number(e.target.value))}>
                <option value={0}>Keep original size</option>
                <option value={4096}>At most 4096 px</option>
                <option value={2560}>At most 2560 px</option>
                <option value={1920}>At most 1920 px (Full HD)</option>
                <option value={1280}>At most 1280 px</option>
              </select>
            </label>
            <label>Video resolution
              <select value={opts.videoHeight} onChange={(e) => set('videoHeight', Number(e.target.value))}>
                <option value={0}>Keep original</option>
                <option value={1080}>1080p at most</option>
                <option value={720}>720p at most</option>
                <option value={480}>480p at most</option>
              </select>
            </label>
            <label>Audio quality
              <select value={opts.audioBitrate} onChange={(e) => set('audioBitrate', Number(e.target.value))}>
                <option value={0}>Automatic</option>
                <option value={192}>192 kbps (high)</option>
                <option value={128}>128 kbps</option>
                <option value={96}>96 kbps</option>
                <option value={64}>64 kbps (voice)</option>
              </select>
            </label>
          </div>
          <p className="muted small">
            Videos come back as MP4. WAV and other lossless audio becomes MP3 (or lossless FLAC with "Best quality").
            Files that can't be shrunk are returned unchanged, so nothing ever gets bigger.
          </p>
        </details>

        {error && <p className="error">{error}</p>}
        {progress && <UploadProgress sent={progress.sentBytes} total={progress.totalBytes} done={progress.filesDone}
          count={progress.filesTotal} failed={progress.errors.length} />}
        <button disabled={busy || !files.length} onClick={start}>{busy ? 'Uploading…' : 'Upload and compress'}</button>
        {busy && <p className="muted small">Keep this tab open until the upload finishes. After that you can close it; compression continues on the server.</p>}
      </div>
    </section>
  );
}
