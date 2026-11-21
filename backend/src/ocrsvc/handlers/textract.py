"""Textract workers.

start:   consumes the textract queue. Small images use synchronous DetectDocumentText;
         everything else starts an async StartDocumentTextDetection job that notifies
         an SNS topic when finished. Concurrency is capped on the event source so we
         stay inside Textract's TPS / concurrent-job quotas; throttling errors are retried.
collect: consumes the SNS -> SQS completion queue, pages through the results and
         writes the unit output.
"""
from __future__ import annotations

import json
import logging

from botocore.exceptions import ClientError

from .. import aws, config, db, jobs, outputs, queues

log = logging.getLogger("ocr")
log.setLevel(logging.INFO)

RETRYABLE = {"ProvisionedThroughputExceededException", "ThrottlingException", "LimitExceededException",
             "InternalServerError", "ServiceUnavailableException", "TooManyRequestsException"}


def _doc_location(key: str) -> dict:
    return {"S3Object": {"Bucket": config.DATA_BUCKET(), "Name": key}}


# ---------------------------------------------------------------- start
def start_handler(event, context):
    return queues.run_batch(event, start)


def start(msg: dict) -> None:
    job_id, doc_id, unit = msg["jobId"], msg["docId"], int(msg["unit"])
    doc = jobs.get_doc(job_id, doc_id)
    if not doc or doc["status"] != "processing":
        return
    tx = aws.client("textract")
    try:
        if msg.get("mode") == "sync":
            resp = tx.detect_document_text(Document=_doc_location(msg["key"]))
            pages = outputs.textract_blocks_to_pages(resp.get("Blocks", []), int(msg["firstPage"]))
            outputs.write_unit(job_id, doc_id, unit, pages or [{"page": int(msg["firstPage"]), "lines": []}])
            jobs.unit_finished(job_id, doc_id, unit, True, int(msg["pages"]))
            return
        resp = tx.start_document_text_detection(
            DocumentLocation=_doc_location(msg["key"]),
            ClientRequestToken=f"{doc_id}-{unit}"[:64],
            JobTag=f"u{unit}",
            NotificationChannel={"SNSTopicArn": config.TEXTRACT_SNS_TOPIC_ARN(),
                                 "RoleArn": config.TEXTRACT_SNS_ROLE_ARN()},
        )
        db.put({
            "PK": f"TEXTRACT#{resp['JobId']}", "SK": "MAP", "jobId": job_id, "docId": doc_id, "unit": unit,
            "firstPage": int(msg["firstPage"]), "pages": int(msg["pages"]), "startedAt": db.now_iso(),
        })
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code in RETRYABLE:
            raise  # SQS redelivers after the visibility timeout
        log.warning("textract rejected %s/%s/%s: %s", job_id, doc_id, unit, exc)
        jobs.unit_finished(job_id, doc_id, unit, False, int(msg["pages"]), f"Textract: {code}: {exc}")


# ---------------------------------------------------------------- collect
def collect_handler(event, context):
    return queues.run_batch(event, collect)


def _unwrap(msg: dict) -> dict:
    # Raw message delivery gives the Textract JSON directly; otherwise it's inside an SNS envelope.
    if "Message" in msg and isinstance(msg["Message"], str):
        return json.loads(msg["Message"])
    return msg


def collect(raw: dict) -> None:
    msg = _unwrap(raw)
    tjob = msg["JobId"]
    mapping = db.get({"PK": f"TEXTRACT#{tjob}", "SK": "MAP"}, consistent=True)
    if not mapping:
        log.warning("no mapping for Textract job %s", tjob)
        return
    job_id, doc_id, unit = mapping["jobId"], mapping["docId"], int(mapping["unit"])
    pages_n = int(mapping["pages"])
    if msg.get("Status") != "SUCCEEDED":
        jobs.unit_finished(job_id, doc_id, unit, False, pages_n,
                           f"Textract job {msg.get('Status')}: {msg.get('StatusMessage', '')}")
        return

    tx = aws.client("textract")
    blocks: list[dict] = []
    token = None
    warnings = []
    while True:
        kwargs = {"JobId": tjob, "MaxResults": 1000}
        if token:
            kwargs["NextToken"] = token
        resp = tx.get_document_text_detection(**kwargs)
        blocks.extend(b for b in resp.get("Blocks", []) if b.get("BlockType") in ("PAGE", "LINE"))
        warnings.extend(resp.get("Warnings", []))
        token = resp.get("NextToken")
        if not token:
            break
    pages = outputs.textract_blocks_to_pages(blocks, int(mapping["firstPage"]))
    outputs.write_unit(job_id, doc_id, unit, pages)
    if warnings:
        log.info("textract warnings for %s: %s", tjob, warnings[:5])
    jobs.unit_finished(job_id, doc_id, unit, True, pages_n)
