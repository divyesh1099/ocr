import { useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, bytes, type Job } from '../api';
import type { Session } from '../App';
import { fromDataTransfer, fromFileList, type PickedFile, type Progress, uploadAll } from '../upload';

const LANGUAGES: [string, string][] = [
  ['eng', 'English'], ['hin', 'Hindi'], ['mar', 'Marathi'], ['guj', 'Gujarati'], ['ben', 'Bengali'],
  ['tam', 'Tamil'], ['tel', 'Telugu'], ['kan', 'Kannada'], ['mal', 'Malayalam'], ['pan', 'Punjabi'],
  ['urd', 'Urdu'], ['san', 'Sanskrit'], ['eng+hin', 'English + Hindi'], ['eng+mar', 'English + Marathi'],
  ['eng+guj', 'English + Gujarati'], ['hin+mar', 'Hindi + Marathi'],
];
const OK_EXT = /\.(pdf|png|jpe?g|jpe|tiff?|bmp|gif|webp|zip)$/i;

export default function NewJob({ session }: { session: Session }) {
  const { settings, user } = session;
  const nav = useNavigate();
  const [files, setFiles] = useState<PickedFile[]>([]);
  const [name, setName] = useState('');
  const [engine, setEngine] = useState(settings.defaultEngine);
  const [language, setLanguage] = useState('eng');
  const [outputs, setOutputs] = useState<string[]>(['txt', 'json']);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [drag, setDrag] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const folderInput = useRef<HTMLInputElement>(null);

  const add = (picked: PickedFile[]) => {
    const good = picked.filter((p) => OK_EXT.test(p.path) && p.file.size > 0);
    setFiles((prev) => [...prev, ...good]);
    if (!name && good.length) setName(good.length === 1 ? good[0].path.split('/').pop()! : good[0].path.split('/')[0]);
  };
  const total = files.reduce((s, f) => s + f.file.size, 0);
  const tooBig = files.filter((f) => f.file.size > settings.maxFileBytes);
  const toggle = (o: string) => setOutputs((cur) => (cur.includes(o) ? cur.filter((x) => x !== o) : [...cur, o]));

  const start = async () => {
    setBusy(true);
    setError('');
    try {
      const { job } = await api<{ job: Job }>('/jobs', { body: { name, engine, language, outputs } });
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

  const price = settings.prices[engine] || 0;
  return (
    <section className="narrow-wide">
      <h1>New OCR job</h1>
      <div
        className={`dropzone${drag ? ' over' : ''}`}
        onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
        onDragLeave={() => setDrag(false)}
        onDrop={async (e) => { e.preventDefault(); setDrag(false); add(await fromDataTransfer(e.dataTransfer)); }}
      >
        <p><b>Drop files or folders here</b></p>
        <p className="muted small">PDF, PNG, JPG, TIFF, BMP, GIF, WEBP, and ZIP archives (nested zips are unpacked)</p>
        <div className="row gap center-x">
          <button type="button" className="ghost" onClick={() => fileInput.current?.click()}>Choose files</button>
          <button type="button" className="ghost" onClick={() => folderInput.current?.click()}>Choose a folder</button>
        </div>
        <input ref={fileInput} type="file" multiple hidden onChange={(e) => { add(fromFileList(e.target.files)); e.target.value = ''; }} />
        <input ref={folderInput} type="file" hidden {...({ webkitdirectory: '', directory: '' } as any)}
          onChange={(e) => { add(fromFileList(e.target.files)); e.target.value = ''; }} />
      </div>

      {files.length > 0 && (
        <div className="card">
          <div className="row between">
            <b>{files.length.toLocaleString()} files · {bytes(total)}</b>
            {!busy && <button className="ghost small" onClick={() => setFiles([])}>Clear</button>}
          </div>
          <ul className="filelist">
            {files.slice(0, 50).map((f, i) => <li key={i}><span>{f.path}</span><span className="muted">{bytes(f.file.size)}</span></li>)}
            {files.length > 50 && <li className="muted">…and {files.length - 50} more</li>}
          </ul>
          {tooBig.length > 0 && <p className="error">{tooBig.length} file(s) are over the {bytes(settings.maxFileBytes)} limit and will be skipped.</p>}
        </div>
      )}

      <div className="card form">
        <label>Job name<input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. 2024 invoices" /></label>
        <label>Engine
          <select value={engine} onChange={(e) => setEngine(e.target.value)}>
            {settings.enabledEngines.includes('textract') && <option value="textract">AWS Textract: best accuracy{settings.billingEnabled ? ` (${settings.prices.textract} credits/page)` : ''}</option>}
            {settings.enabledEngines.includes('tesseract') && <option value="tesseract">Tesseract: bulk + Indian languages{settings.billingEnabled ? ` (${settings.prices.tesseract} credits/page)` : ''}</option>}
          </select>
        </label>
        {engine === 'tesseract' && (
          <label>Language
            <select value={language} onChange={(e) => setLanguage(e.target.value)}>
              {LANGUAGES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          </label>
        )}
        <fieldset>
          <legend>Outputs</legend>
          <label className="check"><input type="checkbox" checked={outputs.includes('txt')} onChange={() => toggle('txt')} /> Plain text (.txt)</label>
          <label className="check"><input type="checkbox" checked={outputs.includes('json')} onChange={() => toggle('json')} /> JSON with lines, confidence and positions</label>
          <label className={`check${engine !== 'tesseract' ? ' disabled' : ''}`}>
            <input type="checkbox" disabled={engine !== 'tesseract'} checked={engine === 'tesseract' && outputs.includes('pdf')} onChange={() => toggle('pdf')} />
            Searchable PDF {engine !== 'tesseract' && <span className="muted small">(Tesseract only)</span>}
          </label>
        </fieldset>
        {settings.billingEnabled && !user.unlimited && (
          <p className="muted small">
            Costs {price} credit{price === 1 ? '' : 's'} per page. Credits are reserved when a file starts and refunded
            for pages that fail. You have {user.credits.toLocaleString()} credits.
          </p>
        )}
        {error && <p className="error">{error}</p>}
        {progress && (
          <div className="progress">
            <div className="bar"><span style={{ width: `${Math.min(100, (progress.sentBytes / Math.max(1, progress.totalBytes)) * 100)}%` }} /></div>
            <p className="small muted">
              Uploaded {bytes(progress.sentBytes)} of {bytes(progress.totalBytes)} · {progress.filesDone}/{progress.filesTotal} files
              {progress.errors.length > 0 && ` · ${progress.errors.length} failed`}
            </p>
          </div>
        )}
        <button disabled={busy || !files.length || !outputs.length} onClick={start}>
          {busy ? 'Uploading…' : `Upload and start OCR`}
        </button>
        {busy && <p className="muted small">Keep this tab open until the upload finishes. After that you can close it; processing continues on the server.</p>}
      </div>
    </section>
  );
}
