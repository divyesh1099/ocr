/** Browser -> S3 uploads with presigned URLs: single PUT for small files, parallel multipart for big ones. */
import { api } from './api';

export interface PickedFile {
  file: File;
  path: string; // relative path, keeps folder structure
}

export interface Progress {
  totalBytes: number;
  sentBytes: number;
  filesDone: number;
  filesTotal: number;
  skipped: string[];
  errors: string[];
}

const BATCH = 200;
const FILE_CONCURRENCY = 4;
const PART_CONCURRENCY = 6;

// ------------------------------------------------------------------ picking files
export function fromFileList(list: FileList | null): PickedFile[] {
  return Array.from(list || []).map((f) => ({ file: f, path: (f as any).webkitRelativePath || f.name }));
}

/** Drag & drop that understands folders (recursively) via the FileSystem entries API. */
export async function fromDataTransfer(dt: DataTransfer): Promise<PickedFile[]> {
  const entries = Array.from(dt.items)
    .map((i) => (i.webkitGetAsEntry ? i.webkitGetAsEntry() : null))
    .filter(Boolean) as FileSystemEntry[];
  if (!entries.length) return fromFileList(dt.files);
  const out: PickedFile[] = [];
  const walk = async (entry: FileSystemEntry, prefix: string): Promise<void> => {
    if (entry.isFile) {
      const file = await new Promise<File>((res, rej) => (entry as FileSystemFileEntry).file(res, rej));
      out.push({ file, path: prefix + file.name });
    } else if (entry.isDirectory) {
      const reader = (entry as FileSystemDirectoryEntry).createReader();
      let batch: FileSystemEntry[];
      do {
        batch = await new Promise<FileSystemEntry[]>((res, rej) => reader.readEntries(res, rej));
        for (const child of batch) await walk(child, `${prefix}${entry.name}/`);
      } while (batch.length);
    }
  };
  for (const e of entries) await walk(e, '');
  return out;
}

// ------------------------------------------------------------------ uploading
function put(url: string, body: Blob, contentType: string | undefined, onProgress: (n: number) => void): Promise<string> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('PUT', url);
    if (contentType) xhr.setRequestHeader('Content-Type', contentType);
    let last = 0;
    xhr.upload.onprogress = (e) => { onProgress(e.loaded - last); last = e.loaded; };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress(body.size - last);
        resolve(xhr.getResponseHeader('ETag') || '');
      } else reject(new Error(`upload failed (${xhr.status})`));
    };
    xhr.onerror = () => reject(new Error('network error while uploading'));
    xhr.send(body);
  });
}

async function retry<T>(fn: () => Promise<T>, tries = 4): Promise<T> {
  let err: unknown;
  for (let i = 0; i < tries; i++) {
    try { return await fn(); } catch (e) { err = e; await new Promise((r) => setTimeout(r, 1000 * 2 ** i)); }
  }
  throw err;
}

async function pool<T>(items: T[], size: number, fn: (item: T) => Promise<void>) {
  let i = 0;
  await Promise.all(Array.from({ length: Math.min(size, items.length) }, async () => {
    while (i < items.length) await fn(items[i++]);
  }));
}

export async function uploadAll(jobId: string, files: PickedFile[], onProgress: (p: Progress) => void) {
  const p: Progress = {
    totalBytes: files.reduce((s, f) => s + f.file.size, 0), sentBytes: 0,
    filesDone: 0, filesTotal: files.length, skipped: [], errors: [],
  };
  const tick = (n = 0) => { p.sentBytes += n; onProgress({ ...p }); };
  // de-duplicate relative paths
  const seen = new Map<string, number>();
  for (const f of files) {
    const n = seen.get(f.path) || 0;
    seen.set(f.path, n + 1);
    if (n) f.path = f.path.replace(/(\.[^./]+)?$/, ` (${n})$1`);
  }

  for (let start = 0; start < files.length; start += BATCH) {
    const batch = files.slice(start, start + BATCH);
    const { uploads } = await api<{ uploads: any[] }>(`/jobs/${jobId}/uploads`, {
      body: { files: batch.map((f) => ({ path: f.path, size: f.file.size })) },
    });
    await pool(uploads.map((u, i) => ({ u, f: batch[i] })), FILE_CONCURRENCY, async ({ u, f }) => {
      try {
        if (u.error) {
          (u.skipped ? p.skipped : p.errors).push(`${u.path}: ${u.error}`);
          tick(f.file.size);
        } else if (u.method === 'single') {
          await retry(() => put(u.url, f.file, u.contentType, tick));
        } else {
          const parts: { PartNumber: number; ETag: string }[] = [];
          const nums = u.partUrls.map((_: string, i: number) => i);
          await pool(nums, PART_CONCURRENCY, async (i: number) => {
            const blob = f.file.slice(i * u.partSize, Math.min((i + 1) * u.partSize, f.file.size));
            const etag = await retry(() => put(u.partUrls[i], blob, undefined, tick));
            parts.push({ PartNumber: i + 1, ETag: etag });
          });
          await api(`/jobs/${jobId}/uploads/complete`, { body: { key: u.key, uploadId: u.uploadId, parts } });
        }
      } catch (e: any) {
        p.errors.push(`${f.path}: ${e.message || e}`);
        if (u.method === 'multipart') {
          api(`/jobs/${jobId}/uploads/abort`, { body: { key: u.key, uploadId: u.uploadId } }).catch(() => undefined);
        }
      }
      p.filesDone++;
      tick();
    });
  }
  return p;
}
