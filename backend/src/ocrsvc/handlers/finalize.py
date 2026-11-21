"""Finalize queue worker.

finalize (default): streams every document output plus a manifest.csv into one zip
                    (results/<jobId>/<jobId>.zip) and marks the job done.
delete:             removes a job's uploads, intermediates, results and DynamoDB items.
"""
from __future__ import annotations

import csv
import io
import logging
import zipfile

from .. import aws, config, db, jobs, outputs, queues, s3io

log = logging.getLogger("ocr")
log.setLevel(logging.INFO)

MANIFEST_FIELDS = ["path", "status", "pages", "engine", "error", "txt", "json", "pdf"]


def handler(event, context):
    outputs.clean_tmp()
    return queues.run_batch(event, handle)


def handle(msg: dict) -> None:
    if msg.get("type") == "delete":
        delete_job(msg["jobId"])
    else:
        finalize(msg["jobId"])


def finalize(job_id: str) -> None:
    job = jobs.get_job(job_id, consistent=True)
    if not job or job["status"] != "finalizing":
        return
    bucket = config.DATA_BUCKET()
    zip_key = f"{config.RESULT_PREFIX}/{job_id}/{job_id}.zip"
    files_prefix = f"{config.RESULT_PREFIX}/{job_id}/files/"
    manifest = io.StringIO()
    mw = csv.DictWriter(manifest, fieldnames=MANIFEST_FIELDS)
    mw.writeheader()

    writer = s3io.S3MultipartWriter(bucket, zip_key, "application/zip")
    try:
        with zipfile.ZipFile(writer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for doc in db.query_all(f"JOB#{job_id}", "DOC#"):
                outs = doc.get("outputs") or {}
                row = {"path": doc["relPath"], "status": doc["status"], "pages": doc.get("pages", 0),
                       "engine": job["engine"], "error": doc.get("error", "")}
                for fmt, key in outs.items():
                    arc = key[len(files_prefix):] if key.startswith(files_prefix) else key.rsplit("/", 1)[-1]
                    row[fmt] = arc
                    body = aws.client("s3").get_object(Bucket=bucket, Key=key)["Body"]
                    with zf.open(arc, "w", force_zip64=True) as dst:
                        while chunk := body.read(8 * 1024 * 1024):
                            dst.write(chunk)
                mw.writerow(row)
            zf.writestr("manifest.csv", manifest.getvalue())
        writer.close()
    except Exception:
        writer.abort()
        raise

    db.update(db.job_key(job_id), "SET #s = :d, resultKey = :k, resultBytes = :b, finishedAt = :t",
              {":d": "done", ":k": zip_key, ":b": writer.written, ":t": db.now_iso(), ":f": "finalizing"},
              names={"#s": "status"}, condition="#s = :f")
    _delete_prefix(bucket, f"{config.WORK_PREFIX}/{job_id}/")
    log.info("job %s finalized (%s bytes)", job_id, writer.written)


def _delete_prefix(bucket: str, prefix: str) -> int:
    s3 = aws.client("s3")
    batch, n = [], 0
    for obj in s3io.iter_keys(bucket, prefix):
        batch.append({"Key": obj["Key"]})
        if len(batch) == 1000:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})
            n += len(batch)
            batch = []
    if batch:
        s3.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})
        n += len(batch)
    return n


def delete_job(job_id: str) -> None:
    job = jobs.get_job(job_id, consistent=True)
    if not job:
        return
    bucket = config.DATA_BUCKET()
    for prefix in (jobs.upload_prefix(job["owner"], job_id), f"{config.WORK_PREFIX}/{job_id}/",
                   f"{config.RESULT_PREFIX}/{job_id}/"):
        _delete_prefix(bucket, prefix)
    with db.table().batch_writer() as bw:
        for item in db.query_all(f"JOB#{job_id}"):
            if item["SK"] != "META":
                bw.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
    db.update(db.job_key(job_id), "SET #s = :x, purgedAt = :t", {":x": "deleted", ":t": db.now_iso()},
              names={"#s": "status"})
