import { useRef, useState } from 'react';
import { bytes } from '../api';
import { fromDataTransfer, fromFileList, type PickedFile } from '../upload';

/** Drop zone + "choose files / folder" buttons + the picked-file list. Shared by OCR and Compress. */
export default function FilePicker({
  files, onAdd, onClear, accept, hint, busy, maxFileBytes,
}: {
  files: PickedFile[];
  onAdd: (picked: PickedFile[]) => void;
  onClear: () => void;
  /** Only keep files whose path matches; omit to accept everything. */
  accept?: RegExp;
  hint: string;
  busy: boolean;
  maxFileBytes: number;
}) {
  const [drag, setDrag] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const folderInput = useRef<HTMLInputElement>(null);
  const add = (picked: PickedFile[]) => onAdd(picked.filter((p) => p.file.size > 0 && (!accept || accept.test(p.path))));
  const total = files.reduce((s, f) => s + f.file.size, 0);
  const tooBig = files.filter((f) => f.file.size > maxFileBytes);

  return (
    <>
      <div
        className={`dropzone${drag ? ' over' : ''}`}
        onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
        onDragLeave={() => setDrag(false)}
        onDrop={async (e) => { e.preventDefault(); setDrag(false); add(await fromDataTransfer(e.dataTransfer)); }}
      >
        <p><b>Drop files or folders here</b></p>
        <p className="muted small">{hint}</p>
        <div className="row gap center-x">
          <button type="button" className="ghost" disabled={busy} onClick={() => fileInput.current?.click()}>Choose files</button>
          <button type="button" className="ghost" disabled={busy} onClick={() => folderInput.current?.click()}>Choose a folder</button>
        </div>
        <input ref={fileInput} type="file" multiple hidden onChange={(e) => { add(fromFileList(e.target.files)); e.target.value = ''; }} />
        <input ref={folderInput} type="file" hidden {...({ webkitdirectory: '', directory: '' } as any)}
          onChange={(e) => { add(fromFileList(e.target.files)); e.target.value = ''; }} />
      </div>

      {files.length > 0 && (
        <div className="card">
          <div className="row between">
            <b>{files.length.toLocaleString()} files · {bytes(total)}</b>
            {!busy && <button className="ghost small" onClick={onClear}>Clear</button>}
          </div>
          <ul className="filelist">
            {files.slice(0, 50).map((f, i) => <li key={i}><span>{f.path}</span><span className="muted">{bytes(f.file.size)}</span></li>)}
            {files.length > 50 && <li className="muted">…and {files.length - 50} more</li>}
          </ul>
          {tooBig.length > 0 && <p className="error">{tooBig.length} file(s) are over the {bytes(maxFileBytes)} limit and will be skipped.</p>}
        </div>
      )}
    </>
  );
}

/** Upload progress bar used under the start button. */
export function UploadProgress({ sent, total, done, count, failed }: {
  sent: number; total: number; done: number; count: number; failed: number;
}) {
  return (
    <div className="progress">
      <div className="bar"><span style={{ width: `${Math.min(100, (sent / Math.max(1, total)) * 100)}%` }} /></div>
      <p className="small muted">
        Uploaded {bytes(sent)} of {bytes(total)} · {done}/{count} files{failed > 0 && ` · ${failed} failed`}
      </p>
    </div>
  );
}
