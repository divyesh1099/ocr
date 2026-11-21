"""End-to-end pipeline test on moto: upload -> submit -> ingest (incl. nested zip) -> process ->
Textract (faked) -> assemble -> finalize, checking counters, credits and the result zip."""
import io
import json
import zipfile

import boto3
from conftest import api_event, drain, sqs_event
from pypdf import PdfWriter

from ocrsvc import aws as aws_mod
from ocrsvc import db, jobs, settings, users
from ocrsvc.handlers import api, assemble, finalize, ingest, process, textract


def make_pdf(pages: int) -> bytes:
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


class FakeTextract:
    def __init__(self):
        self.jobs = {}

    def start_document_text_detection(self, DocumentLocation, ClientRequestToken, **kw):
        jid = f"tj-{ClientRequestToken}"
        self.jobs[jid] = DocumentLocation["S3Object"]["Name"]
        return {"JobId": jid}

    def get_document_text_detection(self, JobId, **kw):
        return {"Blocks": [
            {"BlockType": "PAGE", "Page": 1},
            {"BlockType": "LINE", "Page": 1, "Text": f"hello from {self.jobs[JobId].rsplit('/', 1)[-1]}",
             "Confidence": 99.1, "Geometry": {"BoundingBox": {"Left": 0.1, "Top": 0.1, "Width": 0.5, "Height": 0.05}}},
        ]}

    def detect_document_text(self, Document):
        return {"Blocks": [{"BlockType": "LINE", "Page": 1, "Text": "image text", "Confidence": 97.0,
                            "Geometry": {"BoundingBox": {"Left": 0, "Top": 0, "Width": 1, "Height": 1}}}]}


def call(method, path, sub, email="", body=None, query=None):
    resp = api.handler(api_event(method, path, sub, email, body, query), None)
    return resp["statusCode"], json.loads(resp["body"])


def test_full_pipeline(aws, monkeypatch):
    urls = aws
    fake = FakeTextract()
    real_client = aws_mod.client
    monkeypatch.setattr(aws_mod, "client", lambda name: fake if name == "textract" else real_client(name))
    settings.save({"accessMode": "open", "billingEnabled": True, "freeCreditsOnSignup": 100})

    users.create_profile("u1", "alice@example.com")
    assert users.get_profile("u1")["credits"] == 100

    code, body = call("POST", "/api/jobs", "u1", body={"name": "test", "engine": "textract",
                                                       "outputs": ["txt", "json"]})
    assert code == 201, body
    job_id = body["job"]["jobId"]

    code, body = call("POST", f"/api/jobs/{job_id}/uploads", "u1",
                      body={"files": [{"path": "folder/a.pdf", "size": 10}, {"path": "notes.docx", "size": 5}]})
    assert code == 200
    assert body["uploads"][0]["method"] == "single"
    assert body["uploads"][1].get("skipped")

    # Simulate the browser uploads (a folder file + a zip containing a nested zip)
    s3 = boto3.client("s3")
    prefix = jobs.upload_prefix("u1", job_id)
    s3.put_object(Bucket="ocr-data", Key=prefix + "folder/a.pdf", Body=make_pdf(3))
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr("deep/c.pdf", make_pdf(2))
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w") as z:
        z.writestr("b.pdf", make_pdf(1))
        z.writestr("readme.txt", "not ocr-able")
        z.writestr("inner.zip", inner.getvalue())
    s3.put_object(Bucket="ocr-data", Key=prefix + "archive.zip", Body=outer.getvalue())

    code, body = call("POST", f"/api/jobs/{job_id}/submit", "u1")
    assert code == 200 and body["job"]["status"] == "processing"

    # Run the pipeline until every queue is empty
    for _ in range(20):
        moved = 0
        for name, fn in (("ingest", ingest.handler), ("process", process.handler),
                         ("textract", textract.start_handler), ("assemble", assemble.handler),
                         ("finalize", finalize.handler)):
            msgs = drain(urls, name)
            moved += len(msgs)
            if msgs:
                assert fn(sqs_event(msgs), None) == {"batchItemFailures": []}
        # Fake the Textract SNS notifications for jobs started so far
        notes = [{"JobId": j, "Status": "SUCCEEDED"} for j in list(fake.jobs)]
        done = getattr(fake, "notified", set())
        notes = [n for n in notes if n["JobId"] not in done]
        if notes:
            assert textract.collect_handler(sqs_event(notes), None) == {"batchItemFailures": []}
            fake.notified = done | {n["JobId"] for n in notes}
            moved += len(notes)
        if not moved:
            break

    job = jobs.get_job(job_id, consistent=True)
    assert job["status"] == "done", job
    assert job["pending"] == 0
    assert job["docsDone"] == 3  # a.pdf, b.pdf, inner.zip/deep/c.pdf
    assert job["docsSkipped"] == 1  # readme.txt
    assert job["pagesDone"] == 6
    assert users.get_profile("u1")["credits"] == 100 - 6 * 2  # 2 credits/page for Textract

    zbytes = s3.get_object(Bucket="ocr-data", Key=job["resultKey"])["Body"].read()
    with zipfile.ZipFile(io.BytesIO(zbytes)) as z:
        names = set(z.namelist())
        assert "folder/a.pdf.txt" in names
        assert "archive.zip/inner.zip/deep/c.pdf.json" in names
        assert "manifest.csv" in names
        assert "hello from" in z.read("folder/a.pdf.txt").decode()
        manifest = z.read("manifest.csv").decode()
        assert "readme.txt" in manifest and "skipped" in manifest

    code, body = call("GET", f"/api/jobs/{job_id}/download", "u1")
    assert code == 200 and body["url"].startswith("https://")
    # another user can't see it
    users.create_profile("u2", "bob@example.com")
    code, _ = call("GET", f"/api/jobs/{job_id}", "u2")
    assert code == 404


def test_insufficient_credits_fails_doc_and_finishes(aws, monkeypatch):
    urls = aws
    settings.save({"accessMode": "open", "billingEnabled": True, "freeCreditsOnSignup": 3})
    users.create_profile("u1", "a@example.com")
    job = jobs.create_job("u1", "big", "textract", ["txt"])
    boto3.client("s3").put_object(Bucket="ocr-data", Key=jobs.upload_prefix("u1", job["jobId"]) + "big.pdf",
                                  Body=make_pdf(5))
    assert jobs.submit(job["jobId"])
    ingest.handler(sqs_event(drain(urls, "ingest")), None)
    ingest.handler(sqs_event(drain(urls, "ingest")), None)
    process.handler(sqs_event(drain(urls, "process")), None)
    finalize.handler(sqs_event(drain(urls, "finalize")), None)
    j = jobs.get_job(job["jobId"], consistent=True)
    assert j["status"] == "done" and j["docsFailed"] == 1
    assert users.get_profile("u1")["credits"] == 3  # nothing charged
    doc = next(db.query_all(f"JOB#{job['jobId']}", "DOC#"))
    assert "Not enough credits" in doc["error"]


def test_duplicate_messages_do_not_double_count(aws):
    job = jobs.create_job("u1", "x", "textract", ["txt"])
    jid = job["jobId"]
    db.update(db.job_key(jid), "SET #s = :p, pending = :one", {":p": "processing", ":one": 1}, names={"#s": "status"})
    assert jobs.task_expand(jid, "t1", 2)
    assert not jobs.task_expand(jid, "t1", 2)  # redelivery
    jobs.task_done(jid, "t1")
    jobs.task_done(jid, "t1")
    assert jobs.get_job(jid, consistent=True)["pending"] == 2
