"""Tesseract worker (runs in the container image built from backend/tesseract/Dockerfile).

One SQS message = one unit (an image, a TIFF, or a PDF chunk of up to 10 pages).
PDF pages are rendered with poppler's pdftoppm at 300 DPI, then Tesseract produces
plain text, word boxes (TSV) and, if requested, a searchable PDF in one pass.
"""
from __future__ import annotations

import csv
import glob
import logging
import os
import re
import subprocess

from .. import config, jobs, outputs, queues, s3io

log = logging.getLogger("ocr")
log.setLevel(logging.INFO)

DPI = int(os.environ.get("TESSERACT_DPI", "300"))
LANG_RE = re.compile(r"^[a-z_]{3,8}(\+[a-z_]{3,8}){0,3}$")
TIMEOUT = 840


def handler(event, context):
    outputs.clean_tmp()
    return queues.run_batch(event, handle)


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)  # noqa: S603 - fixed binaries
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed ({proc.returncode}): {proc.stderr[-800:]}")


def handle(msg: dict) -> None:
    job_id, doc_id, unit = msg["jobId"], msg["docId"], int(msg["unit"])
    doc = jobs.get_doc(job_id, doc_id)
    if not doc or doc["status"] != "processing":
        return
    lang = msg.get("lang") or "eng"
    if not LANG_RE.match(lang):
        lang = "eng"
    first_page, n_pages = int(msg["firstPage"]), int(msg["pages"])
    try:
        with outputs.TempDir() as tmp:
            src = s3io.download(config.DATA_BUCKET(), msg["key"], os.path.join(tmp, "in" + _suffix(msg)))
            images = _rasterize(src, msg["kind"], tmp)
            base = os.path.join(tmp, "out")
            configs = ["tsv"] + (["pdf"] if msg.get("pdf") else [])
            if len(images) == 1:
                _run(["tesseract", images[0], base, "-l", lang, "--psm", "3"] + configs)
            else:
                listing = os.path.join(tmp, "pages.txt")
                with open(listing, "w") as fh:
                    fh.write("\n".join(images) + "\n")
                _run(["tesseract", listing, base, "-l", lang, "--psm", "3"] + configs)
            pages = parse_tsv(base + ".tsv", first_page)
            outputs.write_unit(job_id, doc_id, unit, pages)
            if msg.get("pdf") and os.path.exists(base + ".pdf"):
                s3io.upload(config.DATA_BUCKET(), outputs.unit_pdf_key(job_id, doc_id, unit), base + ".pdf",
                            "application/pdf")
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        # Permanent problem with this input; anything else propagates so SQS retries (then the DLQ).
        log.warning("tesseract failed for %s/%s/%s: %s", job_id, doc_id, unit, exc)
        jobs.unit_finished(job_id, doc_id, unit, False, n_pages, f"Tesseract: {exc}")
        return
    jobs.unit_finished(job_id, doc_id, unit, True, n_pages)


def _suffix(msg: dict) -> str:
    return {"pdf": ".pdf", "tiff": ".tiff"}.get(msg["kind"], os.path.splitext(msg["key"])[1] or ".img")


def _rasterize(src: str, kind: str, tmp: str) -> list[str]:
    if kind == "pdf":
        prefix = os.path.join(tmp, "page")
        _run(["pdftoppm", "-r", str(DPI), "-png", src, prefix])
        images = sorted(glob.glob(prefix + "*.png"))
        if not images:
            raise RuntimeError("PDF rendered no pages")
        return images
    if kind == "image" and os.path.splitext(src)[1].lower() in (".gif", ".webp"):
        from PIL import Image

        out = os.path.join(tmp, "converted.png")
        with Image.open(src) as im:
            im.convert("RGB").save(out, "PNG")
        return [out]
    return [src]  # png/jpg/bmp/tiff (multi-page TIFF is handled natively by Tesseract)


def parse_tsv(path: str, first_page: int) -> list[dict]:
    """Group Tesseract's word rows into lines with normalised bounding boxes."""
    pages: dict[int, dict] = {}
    lines: dict[tuple, dict] = {}
    sizes: dict[int, tuple[float, float]] = {}
    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
        reader = csv.DictReader(fh, delimiter="\t", quoting=csv.QUOTE_NONE)
        for row in reader:
            try:
                level, page_num = int(row["level"]), int(row["page_num"])
                left, top, width, height = (int(row[k]) for k in ("left", "top", "width", "height"))
            except (KeyError, TypeError, ValueError):
                continue
            page_no = page_num + first_page - 1
            pages.setdefault(page_no, {"page": page_no, "lines": []})
            if level == 1:
                sizes[page_no] = (float(width) or 1.0, float(height) or 1.0)
                continue
            text = (row.get("text") or "").strip()
            if level != 5 or not text:
                continue
            key = (page_no, row["block_num"], row["par_num"], row["line_num"])
            line = lines.get(key)
            conf = float(row.get("conf") or 0)
            if line is None:
                line = {"words": [], "confs": [], "box": [left, top, left + width, top + height]}
                lines[key] = line
                pages[page_no]["lines"].append(line)
            line["words"].append(text)
            line["confs"].append(conf)
            b = line["box"]
            line["box"] = [min(b[0], left), min(b[1], top), max(b[2], left + width), max(b[3], top + height)]
    result = []
    for page_no in sorted(pages):
        w, h = sizes.get(page_no, (1.0, 1.0))
        page = {"page": page_no, "width": int(w), "height": int(h), "lines": []}
        for line in pages[page_no]["lines"]:
            x0, y0, x1, y1 = line["box"]
            confs = [c for c in line["confs"] if c >= 0]
            page["lines"].append({
                "text": " ".join(line["words"]),
                "conf": round(sum(confs) / len(confs), 2) if confs else 0.0,
                "bbox": [round(x0 / w, 5), round(y0 / h, 5), round((x1 - x0) / w, 5), round((y1 - y0) / h, 5)],
            })
        result.append(page)
    return result
