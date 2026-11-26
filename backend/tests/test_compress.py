"""Compression jobs end to end on moto: create -> upload -> ingest (incl. zip) -> process -> compress -> finalize.
Uses real Pillow / PyMuPDF / pikepdf / ffmpeg where installed (ffmpeg cases are skipped without it)."""
import csv
import io
import json
import math
import shutil
import struct
import subprocess
import wave
import zipfile

import boto3
import pytest
from conftest import api_event, drain, sqs_event
from PIL import Image

from ocrsvc import jobs, users
from ocrsvc.handlers import api, compress, finalize, ingest, process

HAS_FFMPEG = shutil.which("ffmpeg") is not None


def call(method, path, sub, email="", body=None, query=None):
    resp = api.handler(api_event(method, path, sub, email, body, query), None)
    return resp["statusCode"], json.loads(resp["body"])


def noisy_image(w=1600, h=1200) -> Image.Image:
    """Photo-like: smooth colour gradients plus sensor-style noise."""
    noise = Image.effect_noise((w, h), 40).convert("RGB")
    grad = Image.linear_gradient("L").resize((w, h))
    base = Image.merge("RGB", (grad, grad.rotate(90).resize((w, h)), grad.transpose(Image.FLIP_LEFT_RIGHT)))
    return Image.blend(base, noise, 0.35)


def png_bytes() -> bytes:
    buf = io.BytesIO()
    noisy_image().save(buf, "PNG")
    return buf.getvalue()


def jpeg_bytes(q=98) -> bytes:
    buf = io.BytesIO()
    noisy_image().save(buf, "JPEG", quality=q)
    return buf.getvalue()


def pdf_with_image() -> bytes:
    buf = io.BytesIO()
    noisy_image().save(buf, "PDF", resolution=150)
    return buf.getvalue()


def docx_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr("[Content_Types].xml", "<Types/>" * 200)
        z.writestr("word/document.xml", "<w:document>hello</w:document>" * 500)
        z.writestr("word/media/image1.png", png_bytes())
    return buf.getvalue()


def wav_bytes(seconds=3) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(i / 20))) for i in range(44100 * seconds)))
    return buf.getvalue()


def mp4_bytes(tmp_path) -> bytes:
    out = tmp_path / "in.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc2=size=1280x720:rate=30:duration=3", "-c:v", "libx264", "-crf", "10",
                    "-preset", "ultrafast", str(out)], check=True)
    return out.read_bytes()


@pytest.fixture()
def compress_queue(aws, monkeypatch):
    url = boto3.client("sqs").create_queue(QueueName="ocr-compress")["QueueUrl"]
    monkeypatch.setenv("COMPRESS_QUEUE_URL", url)
    return {**aws, "compress": url}


def run_pipeline(urls):
    for _ in range(6):
        msgs = drain(urls, "ingest")
        if not msgs:
            break
        ingest.handler(sqs_event(msgs), None)
    process.handler(sqs_event(drain(urls, "process")), None)
    msgs = drain(urls, "compress")
    for m in msgs:
        assert compress.handler(sqs_event([m]), None) == {"batchItemFailures": []}
    finalize.handler(sqs_event(drain(urls, "finalize")), None)


def test_compress_job(compress_queue, tmp_path):
    urls = compress_queue
    users.create_profile("u1", "boss@example.com")
    code, body = call("GET", "/api/public/config", None)
    assert body["compressEnabled"] is True

    code, body = call("POST", "/api/jobs", "u1", body={"mode": "compress", "name": "shrink",
                                                       "compress": {"level": "strong", "maxImageSide": 1280}})
    assert code == 201, body
    job_id = body["job"]["jobId"]
    assert body["job"]["engine"] == "compress"
    assert body["job"]["compress"]["level"] == "strong"

    files = {
        "scan.pdf": pdf_with_image(),
        "photos/big.png": png_bytes(),
        "photos/camera.jpg": jpeg_bytes(),
        "notes.txt": ("the quick brown fox " * 20000).encode(),
        "report.docx": docx_bytes(),
        "voice.wav": wav_bytes(),
        "tiny.bin": b"\x00\x01",
    }
    if HAS_FFMPEG:
        files["clip.mp4"] = mp4_bytes(tmp_path)
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr("inside/again.png", png_bytes())
    files["bundle.zip"] = inner.getvalue()

    code, body = call("POST", f"/api/jobs/{job_id}/uploads", "u1",
                      body={"files": [{"path": p, "size": len(b)} for p, b in files.items()]})
    assert code == 200
    assert not [u for u in body["uploads"] if u.get("error")], body  # every type is accepted
    s3 = boto3.client("s3")
    prefix = jobs.upload_prefix("u1", job_id)
    for p, b in files.items():
        s3.put_object(Bucket="ocr-data", Key=prefix + p, Body=b)
    code, _ = call("POST", f"/api/jobs/{job_id}/submit", "u1")
    assert code == 200

    run_pipeline(urls)

    job = jobs.get_job(job_id, consistent=True)
    assert job["status"] == "done", job
    assert job["docsFailed"] == 0
    code, body = call("GET", f"/api/jobs/{job_id}/documents", "u1")
    docs = {d["relPath"]: d for d in body["documents"]}
    for path, d in docs.items():
        assert d["status"] == "done", (path, d)
        assert d["compressedSize"] <= d["size"], path
        assert d["outputs"]["file"]
    # everything compressible actually shrank
    for path in ("scan.pdf", "photos/big.png", "photos/camera.jpg", "notes.txt", "report.docx",
                 "voice.wav", "bundle.zip/inside/again.png"):
        assert docs[path]["compressedSize"] < docs[path]["size"] * 0.9, (path, docs[path])
    assert docs["notes.txt"]["outputs"]["file"].endswith("notes.txt.zip")
    assert docs["voice.wav"]["outputs"]["file"].endswith("voice.mp3") or not HAS_FFMPEG
    assert docs["tiny.bin"]["compressedSize"] == 2  # can't shrink: original kept
    if HAS_FFMPEG:
        assert docs["clip.mp4"]["compressedSize"] < docs["clip.mp4"]["size"]
    assert job["bytesOut"] < job["bytesIn"]

    # resized to the 1280px limit, still a valid PNG
    out = s3.get_object(Bucket="ocr-data", Key=docs["photos/big.png"]["outputs"]["file"])["Body"].read()
    assert max(Image.open(io.BytesIO(out)).size) == 1280
    # the docx is still a valid zip with the same entries
    out = s3.get_object(Bucket="ocr-data", Key=docs["report.docx"]["outputs"]["file"])["Body"].read()
    assert sorted(zipfile.ZipFile(io.BytesIO(out)).namelist()) == sorted(
        ["[Content_Types].xml", "word/document.xml", "word/media/image1.png"])

    # result zip + manifest
    z = zipfile.ZipFile(io.BytesIO(s3.get_object(Bucket="ocr-data", Key=job["resultKey"])["Body"].read()))
    names = set(z.namelist())
    assert "scan.pdf" in names and "photos/big.png" in names and "manifest.csv" in names
    rows = list(csv.DictReader(io.StringIO(z.read("manifest.csv").decode())))
    assert all(r["compressedBytes"] for r in rows)
    code, body = call("GET", f"/api/jobs/{job_id}/download", "u1")
    assert "compressed.zip" in body["url"]


def test_compress_options_validated(compress_queue):
    users.create_profile("u1", "boss@example.com")
    code, body = call("POST", "/api/jobs", "u1", body={"mode": "compress", "compress": {"level": "ultra"}})
    assert code == 400


def test_compress_unavailable_without_queue(aws):
    users.create_profile("u1", "boss@example.com")
    code, body = call("POST", "/api/jobs", "u1", body={"mode": "compress"})
    assert code == 400
    assert "not available" in body["error"]
