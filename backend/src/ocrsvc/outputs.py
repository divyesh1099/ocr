"""Unit output format shared by the Textract and Tesseract workers.

Each unit writes work/<jobId>/<docId>/<unit:05d>.json:
  {"pages": [{"page": 1, "width": null, "height": null,
              "text": "...", "lines": [{"text": "...", "conf": 98.1, "bbox": [l, t, w, h]}]}]}
bbox values are fractions of the page size (0..1), like Textract's BoundingBox.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile

from . import config, s3io


def work_dir(job_id: str, doc_id: str) -> str:
    return f"{config.WORK_PREFIX}/{job_id}/{doc_id}"


def unit_json_key(job_id: str, doc_id: str, unit: int) -> str:
    return f"{work_dir(job_id, doc_id)}/{unit:05d}.json"


def unit_pdf_key(job_id: str, doc_id: str, unit: int) -> str:
    return f"{work_dir(job_id, doc_id)}/{unit:05d}.pdf"


def write_unit(job_id: str, doc_id: str, unit: int, pages: list[dict]) -> None:
    for p in pages:
        if "text" not in p:
            p["text"] = "\n".join(line["text"] for line in p.get("lines", []))
    body = json.dumps({"pages": pages}, ensure_ascii=False, separators=(",", ":")).encode()
    s3io.put_bytes(config.DATA_BUCKET(), unit_json_key(job_id, doc_id, unit), body, "application/json")


def textract_blocks_to_pages(blocks: list[dict], first_page: int = 1) -> list[dict]:
    pages: dict[int, dict] = {}
    for b in blocks:
        page_no = int(b.get("Page", 1)) + first_page - 1
        page = pages.setdefault(page_no, {"page": page_no, "lines": []})
        if b.get("BlockType") == "LINE":
            bb = b.get("Geometry", {}).get("BoundingBox", {})
            page["lines"].append({
                "text": b.get("Text", ""),
                "conf": round(float(b.get("Confidence", 0.0)), 2),
                "bbox": [round(bb.get("Left", 0.0), 5), round(bb.get("Top", 0.0), 5),
                         round(bb.get("Width", 0.0), 5), round(bb.get("Height", 0.0), 5)],
            })
    return [pages[k] for k in sorted(pages)]


class TempDir:
    """A scratch dir under /tmp that is always cleaned up (Lambda reuses /tmp between invocations)."""

    def __enter__(self) -> str:
        self.path = tempfile.mkdtemp(prefix="ocr-", dir=os.environ.get("TMPDIR", "/tmp"))
        return self.path

    def __exit__(self, *exc) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def clean_tmp() -> None:
    root = os.environ.get("TMPDIR", "/tmp")
    for name in os.listdir(root):
        if name.startswith("ocr-"):
            shutil.rmtree(os.path.join(root, name), ignore_errors=True)
