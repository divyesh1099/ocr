"""Dead-letter handler: a message that failed repeatedly must not leave its job hanging.

Each failed message is turned into a terminal state for the piece of work it represented,
which releases that work's pending slot so the job can still finish (with errors shown).
"""
from __future__ import annotations

import json
import logging

from .. import db, jobs

log = logging.getLogger("ocr")
log.setLevel(logging.INFO)

REASON = "processing failed repeatedly; see CloudWatch logs"


def handler(event, context):
    for rec in event.get("Records", []):
        source = rec.get("eventSourceARN", "")
        try:
            body = json.loads(rec["body"])
        except json.JSONDecodeError:
            log.error("unparseable DLQ message from %s: %s", source, rec["body"][:500])
            continue
        try:
            handle(source, body)
        except Exception:  # noqa: BLE001 - never loop on the DLQ
            log.exception("could not settle DLQ message from %s: %s", source, body)
    return {}


def handle(source: str, body: dict) -> None:
    if "Message" in body and isinstance(body["Message"], str):  # SNS envelope
        body = json.loads(body["Message"])
    if "JobId" in body and "jobId" not in body:  # Textract completion that couldn't be collected
        mapping = db.get({"PK": f"TEXTRACT#{body['JobId']}", "SK": "MAP"})
        if mapping:
            jobs.unit_finished(mapping["jobId"], mapping["docId"], int(mapping["unit"]), False,
                               int(mapping["pages"]), "collecting Textract results failed repeatedly")
        return

    job_id = body.get("jobId")
    if not job_id:
        return
    if "unit" in body:  # textract/tesseract unit
        jobs.unit_finished(job_id, body["docId"], int(body["unit"]), False, int(body.get("pages", 0)), REASON)
    elif "docId" in body:  # process or assemble
        job = jobs.get_job(job_id)
        doc = jobs.get_doc(job_id, body["docId"], consistent=True)
        if job and doc:
            jobs.refund_failed_pages(job, {**doc, "status": "failed"})
        jobs.doc_finished(job_id, body["docId"], "failed", error=REASON)
    elif "taskId" in body:  # ingest task
        db.update(db.job_key(job_id), "SET lastError = :e", {":e": f"ingest task {body['taskId']} failed: {REASON}"})
        jobs.task_done(job_id, body["taskId"])
    elif body.get("type") == "delete":
        log.error("deleting job %s failed repeatedly", job_id)
    else:  # finalize
        db.update(db.job_key(job_id), "SET #s = :f, lastError = :e", {":f": "failed", ":e": f"finalize: {REASON}"},
                  names={"#s": "status"})
