"""File classification by extension (and magic bytes when available)."""
from __future__ import annotations

import posixpath

PDF = "pdf"
IMAGE = "image"   # single-page raster image
TIFF = "tiff"     # possibly multi-page
ZIP = "zip"
UNSUPPORTED = "unsupported"
FILE = "file"     # any other file: accepted by compress jobs only

COMPRESS = "compress"  # job engine for compression jobs (everything else is OCR)

_EXT = {
    ".pdf": PDF,
    ".png": IMAGE, ".jpg": IMAGE, ".jpeg": IMAGE, ".jpe": IMAGE,
    ".bmp": IMAGE, ".gif": IMAGE, ".webp": IMAGE,
    ".tif": TIFF, ".tiff": TIFF,
    ".zip": ZIP,
}

# Images Textract accepts natively; others are converted to PNG first.
TEXTRACT_NATIVE_IMAGES = {".png", ".jpg", ".jpeg", ".jpe"}

_SKIP_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}


def ext(name: str) -> str:
    return posixpath.splitext(name.lower())[1]


def classify(name: str, head: bytes | None = None) -> str:
    base = posixpath.basename(name).lower()
    if base in _SKIP_NAMES or base.startswith("._") or "__macosx/" in name.lower():
        return UNSUPPORTED
    if head:
        if head.startswith(b"%PDF"):
            return PDF
        if head.startswith(b"PK\x03\x04"):
            return ZIP if ext(name) in ("", ".zip") else _EXT.get(ext(name), UNSUPPORTED)
        if head[:4] in (b"II*\x00", b"MM\x00*"):
            return TIFF
    return _EXT.get(ext(name), UNSUPPORTED)


def classify_for(engine: str, name: str, head: bytes | None = None) -> str:
    """Like classify(), but compress jobs accept every file type (zips are still unpacked)."""
    kind = classify(name, head)
    if engine != COMPRESS or kind != UNSUPPORTED:
        return kind
    base = posixpath.basename(name).lower()
    if base in _SKIP_NAMES or base.startswith("._") or "__macosx/" in name.lower():
        return UNSUPPORTED
    return FILE


def content_type(name: str) -> str:
    return {
        ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".tif": "image/tiff", ".tiff": "image/tiff", ".zip": "application/zip", ".txt": "text/plain; charset=utf-8",
        ".json": "application/json", ".csv": "text/csv; charset=utf-8",
    }.get(ext(name), "application/octet-stream")


def safe_relpath(path: str) -> str:
    """Normalise a user/zip supplied path so it can't escape its prefix."""
    path = path.replace("\\", "/")
    parts = [p for p in path.split("/") if p not in ("", ".", "..")]
    cleaned = "/".join(p.strip()[:200] for p in parts if p.strip())
    return cleaned[:900] or "file"
