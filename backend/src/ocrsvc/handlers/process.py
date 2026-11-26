"""Process queue worker: prepares one document for OCR.

1. counts pages (PDF via pypdf, TIFF via Pillow)
2. reserves credits for those pages (when billing applies)
3. splits the document into OCR units that fit the engine's limits
4. sends each unit to the Textract or Tesseract queue
"""
from __future__ import annotations

import logging
import math
import os

from .. import config, filetypes, jobs, outputs, queues, s3io

log = logging.getLogger("ocr")
log.setLevel(logging.INFO)


class DocError(Exception):
    """A permanent problem with the document (bad file, encrypted, too large...)."""


def handler(event, context):
    outputs.clean_tmp()
    return queues.run_batch(event, handle)


def handle(msg: dict) -> None:
    job = jobs.get_job(msg["jobId"])
    doc = jobs.get_doc(msg["jobId"], msg["docId"], consistent=True)
    if not job or not doc or job.get("deleted") or doc["status"] not in ("queued", "processing"):
        return
    if job["engine"] == filetypes.COMPRESS:
        # One file = one compression task; no page counting, no credits.
        if jobs.start_processing(job["jobId"], doc["docId"], 0, 1):
            queues.send(config.COMPRESS_QUEUE_URL(), {"jobId": job["jobId"], "docId": doc["docId"]})
        return
    try:
        with outputs.TempDir() as tmp:
            units = plan_units(job, doc, tmp)
    except DocError as exc:
        jobs.doc_finished(job["jobId"], doc["docId"], "failed", error=str(exc))
        return
    if not units:
        jobs.doc_finished(job["jobId"], doc["docId"], "skipped", error="document has no pages")
        return
    pages = sum(u["pages"] for u in units)
    if not jobs.reserve_credits(job, doc["docId"], pages):
        jobs.doc_finished(job["jobId"], doc["docId"], "failed",
                          error=f"Not enough credits for {pages} pages. Buy more credits and re-run this file.")
        return
    if not jobs.start_processing(job["jobId"], doc["docId"], pages, len(units)):
        return
    queue = config.TEXTRACT_QUEUE_URL() if job["engine"] == "textract" else config.TESSERACT_QUEUE_URL()
    queues.send_many(queue, (
        {**u, "jobId": job["jobId"], "docId": doc["docId"], "unit": i,
         "lang": job.get("language", "eng"), "pdf": "pdf" in job.get("outputs", [])}
        for i, u in enumerate(units)
    ))


# ---------------------------------------------------------------- planning
def _download(doc: dict, tmp: str) -> str:
    if doc["size"] > config.LOCAL_DOWNLOAD_MAX_BYTES:
        raise DocError("file is too large to process (limit 9 GB per document)")
    path = os.path.join(tmp, "src" + filetypes.ext(doc["relPath"]))
    return s3io.download(config.DATA_BUCKET(), doc["srcKey"], path)


def _open_pdf(path: str):
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(path, strict=False)
        if reader.is_encrypted and not reader.decrypt(""):
            raise DocError("PDF is password protected")
        return reader
    except DocError:
        raise
    except (PdfReadError, ValueError, KeyError, OSError) as exc:
        raise DocError(f"could not read PDF: {exc}") from exc


def _chunk_ranges(total: int, size: int) -> list[tuple[int, int]]:
    return [(s, min(s + size, total)) for s in range(0, total, size)]


def _split_pdf(reader, ranges: list[tuple[int, int]], job_id: str, doc_id: str, tmp: str) -> list[dict]:
    from pypdf import PdfWriter

    bucket = config.DATA_BUCKET()
    units = []
    for i, (start, end) in enumerate(ranges):
        key = f"{outputs.work_dir(job_id, doc_id)}/chunks/{i:05d}.pdf"
        if not s3io.exists(bucket, key):
            writer = PdfWriter()
            for p in range(start, end):
                writer.add_page(reader.pages[p])
            path = os.path.join(tmp, f"chunk{i}.pdf")
            with open(path, "wb") as fh:
                writer.write(fh)
            s3io.upload(bucket, key, path, "application/pdf")
            os.remove(path)
        units.append({"key": key, "firstPage": start + 1, "pages": end - start, "kind": "pdf", "mode": "async"})
    return units


def plan_units(job: dict, doc: dict, tmp: str) -> list[dict]:
    engine, kind, src = job["engine"], doc["kind"], doc["srcKey"]
    job_id, doc_id, size = job["jobId"], doc["docId"], int(doc["size"])

    if kind == filetypes.IMAGE:
        if engine == "tesseract":
            return [{"key": src, "firstPage": 1, "pages": 1, "kind": "image"}]
        key, size = src, size
        if filetypes.ext(doc["relPath"]) not in filetypes.TEXTRACT_NATIVE_IMAGES:
            key, size = _convert_to_png(doc, tmp)
        if size > config.TEXTRACT_ASYNC_MAX_BYTES:
            raise DocError("image is too large for Textract (max 450 MB)")
        mode = "sync" if size <= config.TEXTRACT_SYNC_MAX_BYTES else "async"
        return [{"key": key, "firstPage": 1, "pages": 1, "kind": "image", "mode": mode}]

    if kind == filetypes.TIFF:
        path = _download(doc, tmp)
        n = _tiff_pages(path)
        if engine == "tesseract":
            return [{"key": src, "firstPage": 1, "pages": n, "kind": "tiff"}]
        if n <= config.TEXTRACT_ASYNC_MAX_PAGES and size <= config.TEXTRACT_ASYNC_MAX_BYTES:
            return [{"key": src, "firstPage": 1, "pages": n, "kind": "tiff", "mode": "async"}]
        return _split_tiff(path, n, job_id, doc_id, tmp)

    if kind == filetypes.PDF:
        path = _download(doc, tmp)
        reader = _open_pdf(path)
        n = len(reader.pages)
        if n == 0:
            return []
        if engine == "tesseract":
            if n <= config.TESSERACT_CHUNK_PAGES:
                return [{"key": src, "firstPage": 1, "pages": n, "kind": "pdf"}]
            return _split_pdf(reader, _chunk_ranges(n, config.TESSERACT_CHUNK_PAGES), job_id, doc_id, tmp)
        if n <= config.TEXTRACT_ASYNC_MAX_PAGES and size <= config.TEXTRACT_ASYNC_MAX_BYTES:
            return [{"key": src, "firstPage": 1, "pages": n, "kind": "pdf", "mode": "async"}]
        by_pages = math.ceil(n / config.TEXTRACT_CHUNK_PAGES)
        chunks = max(by_pages, math.ceil(size / config.TEXTRACT_ASYNC_MAX_BYTES * 1.2))
        per_chunk = max(1, math.ceil(n / chunks))
        return _split_pdf(reader, _chunk_ranges(n, per_chunk), job_id, doc_id, tmp)

    raise DocError(f"unsupported document type {kind}")


def _convert_to_png(doc: dict, tmp: str) -> tuple[str, int]:
    from PIL import Image

    path = _download(doc, tmp)
    out = os.path.join(tmp, "converted.png")
    try:
        with Image.open(path) as im:
            im.convert("RGB").save(out, "PNG", optimize=True)
    except Exception as exc:  # noqa: BLE001
        raise DocError(f"could not read image: {exc}") from exc
    key = f"{outputs.work_dir(doc['jobId'], doc['docId'])}/converted.png"
    s3io.upload(config.DATA_BUCKET(), key, out, "image/png")
    return key, os.path.getsize(out)


def _tiff_pages(path: str) -> int:
    from PIL import Image

    try:
        with Image.open(path) as im:
            return int(getattr(im, "n_frames", 1))
    except Exception as exc:  # noqa: BLE001
        raise DocError(f"could not read TIFF: {exc}") from exc


def _split_tiff(path: str, n: int, job_id: str, doc_id: str, tmp: str) -> list[dict]:
    from PIL import Image, ImageSequence

    bucket = config.DATA_BUCKET()
    per_chunk = config.TEXTRACT_CHUNK_PAGES // 2
    units = []
    with Image.open(path) as im:
        frames = ImageSequence.Iterator(im)
        for i, (start, end) in enumerate(_chunk_ranges(n, per_chunk)):
            key = f"{outputs.work_dir(job_id, doc_id)}/chunks/{i:05d}.tiff"
            batch = [frames[p].copy() for p in range(start, end)]
            out = os.path.join(tmp, f"chunk{i}.tiff")
            batch[0].save(out, save_all=True, append_images=batch[1:], compression="tiff_deflate")
            s3io.upload(bucket, key, out, "image/tiff")
            os.remove(out)
            units.append({"key": key, "firstPage": start + 1, "pages": end - start, "kind": "tiff", "mode": "async"})
    return units
