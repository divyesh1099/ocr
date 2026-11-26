"""Compress queue worker: shrinks one file per message.

PDF     three passes, from divyesh1099/compressPDF: PyMuPDF re-encodes images as JPEG, pikepdf/qpdf
        compresses streams and packs objects, Ghostscript rewrites the file with a quality preset.
        The smallest valid result wins.
Image   Pillow: optional resize, then JPEG/WebP quality or lossless PNG/GIF/TIFF optimisation
        (PNGs are colour-quantised at the "strong" level). Optional conversion to WebP or JPEG.
Video   ffmpeg: H.264 (CRF by level) + AAC in .mp4, optionally scaled down.
Audio   ffmpeg: re-encoded at a bitrate; lossless sources become FLAC (light) or MP3.
Office  .docx/.xlsx/.pptx/.odt/.epub...: the images inside are recompressed and the zip repacked.
Other   deflated into a .zip, if that saves at least 5%.

A result that is not smaller than the original is replaced by the original, so nobody ever gets
a bigger file back. The output keeps the file's folder path; only its extension may change.
"""
from __future__ import annotations

import logging
import os
import posixpath
import shutil
import subprocess
import zipfile

from .. import config, db, filetypes, jobs, outputs, queues, s3io

log = logging.getLogger("ocr")
log.setLevel(logging.INFO)

TIMEOUT = int(os.environ.get("COMPRESS_TIMEOUT", "3300"))
LEVELS = ("light", "balanced", "strong")
IMAGE_FORMATS = ("keep", "webp", "jpeg")

# per level: image quality, Ghostscript preset, video CRF, audio kbit/s
PRESETS = {
    "light": {"q": 85, "gs": "/printer", "crf": 23, "abr": 192},
    "balanced": {"q": 72, "gs": "/ebook", "crf": 28, "abr": 128},
    "strong": {"q": 55, "gs": "/screen", "crf": 32, "abr": 96},
}

PDF_EXT = {".pdf"}
IMAGE_EXT = {".jpg", ".jpeg", ".jpe", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
VIDEO_EXT = {".mp4", ".m4v", ".mov", ".mkv", ".avi", ".webm", ".wmv", ".flv", ".3gp", ".mpeg", ".mpg", ".ts",
             ".mts", ".m2ts"}
AUDIO_EXT = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wma", ".aif", ".aiff"}
LOSSLESS_AUDIO = {".wav", ".flac", ".aif", ".aiff"}
OFFICE_EXT = {".docx", ".xlsx", ".pptx", ".docm", ".xlsm", ".pptm", ".odt", ".ods", ".odp", ".odg", ".epub"}
# Already-compressed archives and media we can't shrink further by deflating
NO_DEFLATE_EXT = {".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".zst", ".br", ".lz", ".lzma", ".jar",
                  ".apk", ".heic", ".heif", ".avif", ".jxl"}


class CompressError(Exception):
    """A permanent problem with this file (corrupt, unsupported, a tool refused it)."""


def handler(event, context):
    outputs.clean_tmp()
    return queues.run_batch(event, handle)


def options(job: dict) -> dict:
    raw = job.get("compress") or {}
    level = raw.get("level") if raw.get("level") in LEVELS else "balanced"
    return {
        **PRESETS[level],
        "level": level,
        "imageFormat": raw.get("imageFormat") if raw.get("imageFormat") in IMAGE_FORMATS else "keep",
        "maxImageSide": int(raw.get("maxImageSide") or 0),
        "videoHeight": int(raw.get("videoHeight") or 0),
        "abr": int(raw.get("audioBitrate") or 0) or PRESETS[level]["abr"],
    }


def handle(msg: dict) -> None:
    job_id, doc_id = msg["jobId"], msg["docId"]
    job = jobs.get_job(job_id)
    doc = jobs.get_doc(job_id, doc_id, consistent=True)
    if not job or not doc or doc["status"] != "processing":
        return
    opts = options(job)
    bucket = config.DATA_BUCKET()
    with outputs.TempDir() as tmp:
        ext = filetypes.ext(doc["relPath"])
        src = s3io.download(bucket, doc["srcKey"], os.path.join(tmp, "in" + ext))
        try:
            out, out_ext, note = compress_file(src, ext, opts, tmp, posixpath.basename(doc["relPath"]))
        except CompressError as exc:
            jobs.doc_finished(job_id, doc_id, "failed", error=str(exc))
            return
        before = os.path.getsize(src)
        after = os.path.getsize(out) if out else before
        if not out or after >= before * 0.98:
            out, out_ext, after = src, ext, before
            note = "already as small as it gets; original kept"
        rel = doc["relPath"]
        if out_ext == ".zip" and ext != ".zip":
            rel += ".zip"
        elif out_ext != ext:
            rel = posixpath.splitext(rel)[0] + out_ext
        key = f"{config.RESULT_PREFIX}/{job_id}/files/{rel}"
        s3io.upload(bucket, key, out, filetypes.content_type(rel))

    _record_sizes(job_id, doc_id, before, after, note)
    jobs.doc_finished(job_id, doc_id, "done", outputs={"file": key})


def _record_sizes(job_id: str, doc_id: str, before: int, after: int, note: str) -> None:
    """Store the doc's sizes and add them to the job's totals (once, even on redelivery)."""
    try:
        db.transact([
            db.tx_update(db.doc_key(job_id, doc_id), "SET compressedSize = :a, note = :n",
                         {":a": after, ":n": note}, condition="attribute_not_exists(compressedSize)"),
            db.tx_update(db.job_key(job_id), "ADD bytesIn :b, bytesOut :a", {":b": before, ":a": after}),
        ])
    except db.TxConflict:
        log.info("sizes for %s/%s already recorded", job_id, doc_id)


# ---------------------------------------------------------------- dispatch
def compress_file(src: str, ext: str, opts: dict, tmp: str, name: str = "file") -> tuple[str | None, str, str]:
    """Returns (output path or None, output extension, note)."""
    if ext in PDF_EXT:
        return compress_pdf(src, opts, tmp), ".pdf", ""
    if ext in IMAGE_EXT:
        return compress_image(src, ext, opts, tmp)
    if ext in VIDEO_EXT:
        return compress_video(src, opts, tmp), ".mp4", ""
    if ext in AUDIO_EXT:
        return compress_audio(src, ext, opts, tmp)
    if ext in OFFICE_EXT:
        return compress_office(src, ext, opts, tmp), ext, ""
    if ext in NO_DEFLATE_EXT:
        return None, ext, ""
    return deflate(src, name, tmp), ".zip", "packed into a .zip"


def _run(cmd: list[str]) -> None:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)  # noqa: S603 - fixed binaries
    except FileNotFoundError as exc:
        raise CompressError(f"{cmd[0]} is not installed on this server") from exc
    except subprocess.TimeoutExpired as exc:
        raise CompressError(f"{cmd[0]} took longer than {TIMEOUT // 60} minutes") from exc
    if proc.returncode != 0:
        raise CompressError(f"{cmd[0]} could not process this file: {proc.stderr.strip()[-400:]}")


# ---------------------------------------------------------------- PDF
def compress_pdf(src: str, opts: dict, tmp: str) -> str:
    candidates: list[str] = []
    current = src
    try:  # pass 1: re-encode embedded images
        try:
            import pymupdf as fitz
        except ImportError:
            import fitz

        stage1 = os.path.join(tmp, "s1.pdf")
        _pymupdf_images(src, stage1, opts["q"], fitz)
        candidates.append(stage1)
        current = stage1
    except ImportError:
        log.warning("PyMuPDF not installed; skipping image pass")
    except Exception as exc:  # noqa: BLE001 - try the other passes
        log.warning("PyMuPDF pass failed: %s", exc)
    try:  # pass 2: stream compression + object streams
        import pikepdf

        stage2 = os.path.join(tmp, "s2.pdf")
        with pikepdf.open(current) as pdf:
            if pdf.is_encrypted:
                raise CompressError("PDF is password protected")
            pdf.save(stage2, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.generate,
                     recompress_flate=True)
        candidates.append(stage2)
        current = stage2
    except ImportError:
        log.warning("pikepdf not installed; skipping qpdf pass")
    except CompressError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("pikepdf pass failed: %s", exc)
    if shutil.which("gs"):  # pass 3: Ghostscript rewrite with a quality preset
        stage3 = os.path.join(tmp, "s3.pdf")
        try:
            _run(["gs", "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.6", f"-dPDFSETTINGS={opts['gs']}",
                  "-dDetectDuplicateImages=true", "-dAutoRotatePages=/None", "-dColorImageDownsampleType=/Bicubic",
                  "-dGrayImageDownsampleType=/Bicubic", "-dMonoImageDownsampleType=/Subsample",
                  "-dEmbedAllFonts=true", "-dSubsetFonts=true", "-dCompressFonts=true", "-dFastWebView=true",
                  "-dSAFER", "-dNOPAUSE", "-dBATCH", "-dQUIET", f"-sOutputFile={stage3}", current])
            candidates.append(stage3)
        except CompressError as exc:
            log.warning("Ghostscript pass failed: %s", exc)
    good = [c for c in candidates if os.path.exists(c) and os.path.getsize(c) > 0 and _pdf_ok(c)]
    if not good:
        raise CompressError("could not read this PDF")
    return min(good, key=os.path.getsize)


def _pymupdf_images(src: str, out: str, quality: int, fitz) -> None:
    doc = fitz.open(src)
    if doc.needs_pass:
        doc.close()
        raise CompressError("PDF is password protected")
    seen: set[int] = set()
    for page in doc:
        for xref, *_ in page.get_images(full=True):
            if xref in seen:
                continue
            seen.add(xref)
            try:
                raw = doc.extract_image(xref)
                if not raw:
                    continue
                pix = fitz.Pixmap(doc, xref)
                if pix.alpha or pix.n > 4:
                    continue
                if pix.n == 4:  # CMYK -> RGB
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                new = pix.tobytes("jpeg", jpg_quality=quality)
                if len(new) < len(raw["image"]):
                    page.replace_image(xref, stream=new)
            except Exception as exc:  # noqa: BLE001 - leave that image as it is
                log.debug("image %s kept: %s", xref, exc)
    doc.save(out, garbage=4, deflate=True, clean=True)
    doc.close()


def _pdf_ok(path: str) -> bool:
    try:
        from pypdf import PdfReader

        return len(PdfReader(path, strict=False).pages) > 0
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- images
def compress_image(src: str, ext: str, opts: dict, tmp: str) -> tuple[str, str, str]:
    from PIL import Image, ImageOps

    try:
        im = Image.open(src)
        animated = getattr(im, "is_animated", False)
        if not animated:
            im = ImageOps.exif_transpose(im)
    except Exception as exc:  # noqa: BLE001
        raise CompressError(f"could not read image: {exc}") from exc
    if animated:  # keep frames; only GIF/WebP optimisation
        out = os.path.join(tmp, "out" + ext)
        im.save(out, save_all=True, optimize=True)
        return out, ext, ""

    side = opts["maxImageSide"]
    if side and max(im.size) > side:
        im.thumbnail((side, side), Image.LANCZOS)
    has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
    target, q = opts["imageFormat"], opts["q"]
    if target == "jpeg" and has_alpha:
        target = "keep"  # JPEG can't hold transparency

    if target == "webp" or (target == "keep" and ext == ".webp"):
        out = os.path.join(tmp, "out.webp")
        im.save(out, "WEBP", quality=q, method=6)
        return out, ".webp", ""
    if target == "jpeg" or ext in (".jpg", ".jpeg", ".jpe"):
        out = os.path.join(tmp, "out.jpg")
        im.convert("RGB").save(out, "JPEG", quality=q, optimize=True, progressive=True)
        return out, (ext if target == "keep" else ".jpg"), ""
    if ext in (".tif", ".tiff"):
        out = os.path.join(tmp, "out" + ext)
        if opts["level"] == "strong" and im.mode in ("RGB", "L"):
            im.save(out, "TIFF", compression="jpeg", quality=q)
        else:
            im.save(out, "TIFF", compression="tiff_adobe_deflate")
        return out, ext, ""
    if ext == ".gif":
        out = os.path.join(tmp, "out.gif")
        im.save(out, "GIF", optimize=True)
        return out, ".gif", ""
    # PNG (and BMP, saved as PNG: lossless and far smaller)
    out = os.path.join(tmp, "out.png")
    if opts["level"] == "strong" and im.mode not in ("P", "1"):
        im = im.convert("RGBA" if has_alpha else "RGB").quantize(colors=256, method=Image.Quantize.FASTOCTREE
                                                                  if has_alpha else Image.Quantize.MEDIANCUT)
    im.save(out, "PNG", optimize=True)
    return out, ".png", ("converted to PNG" if ext == ".bmp" else "")


# ---------------------------------------------------------------- video / audio
def compress_video(src: str, opts: dict, tmp: str) -> str:
    out = os.path.join(tmp, "out.mp4")
    vf = ["-vf", f"scale=-2:'min({opts['videoHeight']},ih)'"] if opts["videoHeight"] else []
    _run(["ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", src, "-map", "0:v:0", "-map", "0:a?",
          *vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", str(opts["crf"]), "-pix_fmt", "yuv420p",
          "-c:a", "aac", "-b:a", f"{min(opts['abr'], 160)}k", "-movflags", "+faststart", out])
    return out


def compress_audio(src: str, ext: str, opts: dict, tmp: str) -> tuple[str, str, str]:
    abr = f"{opts['abr']}k"
    if ext in LOSSLESS_AUDIO and opts["level"] == "light":
        out_ext, codec, note = ".flac", ["-c:a", "flac", "-compression_level", "8"], "lossless FLAC"
    elif ext in (".m4a", ".aac"):
        out_ext, codec, note = ".m4a", ["-c:a", "aac", "-b:a", abr], ""
    elif ext in (".ogg", ".oga"):
        out_ext, codec, note = ".ogg", ["-c:a", "libvorbis", "-b:a", abr], ""
    elif ext == ".opus":
        out_ext, codec, note = ".opus", ["-c:a", "libopus", "-b:a", abr], ""
    else:
        note = "converted to MP3" if ext != ".mp3" else ""
        out_ext, codec = ".mp3", ["-c:a", "libmp3lame", "-b:a", abr]
    out = os.path.join(tmp, "out" + out_ext)
    _run(["ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", src, "-vn", "-map_metadata", "0", *codec, out])
    return out, out_ext, note


# ---------------------------------------------------------------- office docs / other files
def compress_office(src: str, ext: str, opts: dict, tmp: str) -> str:
    """Recompress images inside an OOXML/ODF/EPUB zip without changing their names."""
    out = os.path.join(tmp, "out" + ext)
    img_opts = {**opts, "imageFormat": "keep"}
    try:
        with zipfile.ZipFile(src) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zout:
            for info in zin.infolist():
                data = zin.read(info)
                iext = filetypes.ext(info.filename)
                if info.filename == "mimetype":  # ODF/EPUB: must stay first and stored
                    zout.writestr(info, data, compress_type=zipfile.ZIP_STORED)
                    continue
                if iext in (".jpg", ".jpeg", ".png") and len(data) > 20_000:
                    data = _shrink_embedded(data, iext, img_opts, tmp)
                zout.writestr(info.filename, data)
    except zipfile.BadZipFile as exc:
        raise CompressError(f"file is damaged: {exc}") from exc
    return out


def _shrink_embedded(data: bytes, ext: str, opts: dict, tmp: str) -> bytes:
    path = os.path.join(tmp, "embedded" + ext)
    with open(path, "wb") as fh:
        fh.write(data)
    try:
        out, out_ext, _ = compress_image(path, ext, opts, tmp)
    except CompressError:
        return data
    if out_ext != ext:
        return data
    with open(out, "rb") as fh:
        new = fh.read()
    return new if len(new) < len(data) else data


def deflate(src: str, name: str, tmp: str) -> str:
    out = os.path.join(tmp, "out.zip")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9, allowZip64=True) as zf:
        zf.write(src, arcname=name)
    return out
