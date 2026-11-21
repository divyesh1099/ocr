"""Assemble queue worker: merges a document's unit outputs into its final files.

results/<jobId>/files/<relPath>.txt    plain text, pages separated by a header line
results/<jobId>/files/<relPath>.json   pages with lines, confidences and bounding boxes
results/<jobId>/files/<relPath>.pdf    searchable PDF (Tesseract engine, when requested)
"""
from __future__ import annotations

import json
import logging
import os

from .. import config, jobs, outputs, queues, s3io

log = logging.getLogger("ocr")
log.setLevel(logging.INFO)


def handler(event, context):
    outputs.clean_tmp()
    return queues.run_batch(event, handle)


def result_base(job_id: str, rel_path: str) -> str:
    return f"{config.RESULT_PREFIX}/{job_id}/files/{rel_path}"


def handle(msg: dict) -> None:
    job_id, doc_id = msg["jobId"], msg["docId"]
    job = jobs.get_job(job_id)
    doc = jobs.get_doc(job_id, doc_id, consistent=True)
    if not job or not doc or doc["status"] != "assembling":
        return
    bucket = config.DATA_BUCKET()
    base = result_base(job_id, doc["relPath"])
    wanted = job.get("outputs") or ["txt"]
    out_keys: dict[str, str] = {}
    pages_ok = 0

    with outputs.TempDir() as tmp:
        txt_path, json_path = os.path.join(tmp, "doc.txt"), os.path.join(tmp, "doc.json")
        pdf_parts: list[str] = []
        multi = int(doc.get("pages", 1)) > 1
        with open(txt_path, "w", encoding="utf-8") as txt, open(json_path, "w", encoding="utf-8") as js:
            js.write(json.dumps({"source": doc["relPath"], "engine": job["engine"], "pages": doc.get("pages", 0)})[:-1])
            js.write(', "results": [')
            first_page_written = True
            for unit in range(int(doc["unitsTotal"])):
                key = outputs.unit_json_key(job_id, doc_id, unit)
                if not s3io.exists(bucket, key):
                    txt.write(f"\n\n===== OCR failed for part {unit + 1} of this document =====\n\n")
                    continue
                data = json.loads(s3io.get_bytes(bucket, key))
                for page in data.get("pages", []):
                    if multi:
                        txt.write(("" if pages_ok == 0 else "\n\n") + f"===== Page {page['page']} =====\n\n")
                    txt.write(page.get("text", ""))
                    js.write(("" if first_page_written else ",") + json.dumps(page, ensure_ascii=False))
                    first_page_written = False
                    pages_ok += 1
                if "pdf" in wanted and job["engine"] == "tesseract":
                    pkey = outputs.unit_pdf_key(job_id, doc_id, unit)
                    if s3io.exists(bucket, pkey):
                        local = os.path.join(tmp, f"u{unit:05d}.pdf")
                        s3io.download(bucket, pkey, local)
                        pdf_parts.append(local)
            js.write("]}")
            txt.write("\n")

        if "txt" in wanted:
            s3io.upload(bucket, base + ".txt", txt_path, "text/plain; charset=utf-8")
            out_keys["txt"] = base + ".txt"
        if "json" in wanted:
            s3io.upload(bucket, base + ".json", json_path, "application/json")
            out_keys["json"] = base + ".json"
        if pdf_parts:
            merged = os.path.join(tmp, "doc.pdf")
            _merge_pdfs(pdf_parts, merged)
            s3io.upload(bucket, base + ".pdf", merged, "application/pdf")
            out_keys["pdf"] = base + ".pdf"

    status = "done" if int(doc.get("unitsDone", 0)) > 0 else "failed"
    doc_view = {**doc, "status": status}
    kept = jobs.refund_failed_pages(job, doc_view)
    error = doc.get("lastError", "") if int(doc.get("unitsFailed", 0)) else ""
    pages_done = max(0, int(doc.get("pages", 0)) - int(doc.get("failedPages", 0)))
    jobs.doc_finished(job_id, doc_id, status, pages_done=pages_done if status == "done" else 0,
                      error=error, outputs=out_keys if status == "done" else {}, credits=kept)


def _merge_pdfs(parts: list[str], out: str) -> None:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for p in parts:
        writer.append(p)
    with open(out, "wb") as fh:
        writer.write(fh)
