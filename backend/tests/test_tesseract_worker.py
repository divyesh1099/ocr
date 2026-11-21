"""Runs the real Tesseract worker on a generated 2-page PDF (skipped if tesseract/pdftoppm are missing)."""
import io
import json
import shutil

import boto3
import pytest
from conftest import drain, sqs_event
from PIL import Image, ImageDraw, ImageFont

from ocrsvc import db, jobs, outputs
from ocrsvc.handlers import assemble, tesseract

pytestmark = pytest.mark.skipif(not (shutil.which("tesseract") and shutil.which("pdftoppm")),
                                reason="tesseract/poppler not installed")


def text_pdf(lines_per_page: list[str]) -> bytes:
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 48)
    except OSError:
        font = ImageFont.load_default(size=48)
    pages = []
    for text in lines_per_page:
        im = Image.new("RGB", (1700, 2200), "white")
        ImageDraw.Draw(im).text((150, 300), text, fill="black", font=font)
        pages.append(im)
    buf = io.BytesIO()
    pages[0].save(buf, "PDF", resolution=200, save_all=True, append_images=pages[1:])
    return buf.getvalue()


def test_tesseract_unit_and_searchable_pdf(aws):
    urls = aws
    job = jobs.create_job("u1", "t", "tesseract", ["txt", "json", "pdf"])
    jid = job["jobId"]
    key = jobs.upload_prefix("u1", jid) + "scan.pdf"
    boto3.client("s3").put_object(Bucket="ocr-data", Key=key, Body=text_pdf(["Invoice number 4417", "Total due 2500"]))
    db.update(db.job_key(jid), "SET #s = :p, pending = :one", {":p": "processing", ":one": 1}, names={"#s": "status"})
    doc_id, _ = jobs.register_doc(jid, key, "scan.pdf", "pdf", 1000)
    assert jobs.start_processing(jid, doc_id, 2, 1)

    msg = {"jobId": jid, "docId": doc_id, "unit": 0, "key": key, "firstPage": 1, "pages": 2, "kind": "pdf",
           "lang": "eng", "pdf": True}
    assert tesseract.handler(sqs_event([msg]), None) == {"batchItemFailures": []}

    data = json.loads(boto3.client("s3").get_object(Bucket="ocr-data", Key=outputs.unit_json_key(jid, doc_id, 0))
                      ["Body"].read())
    assert [p["page"] for p in data["pages"]] == [1, 2]
    assert "4417" in data["pages"][0]["text"]
    assert "2500" in data["pages"][1]["text"]
    assert all(0 <= v <= 1 for v in data["pages"][0]["lines"][0]["bbox"])

    # unit completion queued the assemble step; run it
    assert assemble.handler(sqs_event(drain(urls, "assemble")), None) == {"batchItemFailures": []}
    doc = jobs.get_doc(jid, doc_id, consistent=True)
    assert doc["status"] == "done" and set(doc["outputs"]) == {"txt", "json", "pdf"}
    txt = boto3.client("s3").get_object(Bucket="ocr-data", Key=doc["outputs"]["txt"])["Body"].read().decode()
    assert "===== Page 2 =====" in txt and "Invoice" in txt
    pdf = boto3.client("s3").get_object(Bucket="ocr-data", Key=doc["outputs"]["pdf"])["Body"].read()
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(pdf))
    assert len(reader.pages) == 2 and "4417" in reader.pages[0].extract_text()
    assert jobs.get_job(jid, consistent=True)["status"] == "finalizing"
