"""Ingest queue worker: discovers documents.

Message types:
  scan      list one page of the job's upload prefix and fan out `file` tasks
  file      classify one S3 object: document -> register + send to process queue,
            zip -> read its central directory and fan out `zipbatch` tasks
  zipbatch  extract a slice of a zip's entries to S3 and register them
"""
from __future__ import annotations

import hashlib
import logging
import posixpath
import zipfile

from .. import aws, config, filetypes, jobs, queues, s3io

log = logging.getLogger("ocr")
log.setLevel(logging.INFO)

ZIP_BATCH_BYTES = 2 * 1024 * 1024 * 1024  # compressed bytes per zip batch


def _h(*parts: object) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:16]


def handler(event, context):
    return queues.run_batch(event, handle)


def handle(msg: dict) -> None:
    job = jobs.get_job(msg["jobId"])
    if not job or job.get("deleted"):
        return
    kind = msg["type"]
    if kind == "scan":
        scan(job, msg)
    elif kind == "file":
        file_task(job, msg)
    elif kind == "zipbatch":
        zip_batch(job, msg)
    else:
        raise ValueError(f"unknown ingest message type {kind}")


# ---------------------------------------------------------------- scan
def scan(job: dict, msg: dict) -> None:
    job_id, task_id = job["jobId"], msg["taskId"]
    bucket = config.DATA_BUCKET()
    prefix = jobs.upload_prefix(job["owner"], job_id)
    objects, next_token = s3io.list_keys(bucket, prefix, msg.get("token"))
    children = [o for o in objects if o["Size"] > 0 and not o["Key"].endswith("/")]
    jobs.task_expand(job_id, task_id, len(children) + (1 if next_token else 0))
    queues.send_many(config.INGEST_QUEUE_URL(), (
        {"type": "file", "jobId": job_id, "taskId": f"f-{_h(o['Key'])}", "key": o["Key"],
         "relPath": o["Key"][len(prefix):], "size": o["Size"], "depth": 0}
        for o in children
    ))
    if next_token:
        queues.send(config.INGEST_QUEUE_URL(), {"type": "scan", "jobId": job_id, "taskId": f"scan-{_h(next_token)}",
                                                "token": next_token})
    jobs.task_done(job_id, task_id)


# ---------------------------------------------------------------- file
def _head(bucket: str, key: str) -> bytes:
    try:
        return aws.client("s3").get_object(Bucket=bucket, Key=key, Range="bytes=0-15")["Body"].read()
    except Exception:  # noqa: BLE001
        return b""


def _register_terminal(job_id: str, key: str, rel: str, kind: str, size: int, status: str, error: str) -> None:
    doc_id, _ = jobs.register_doc(job_id, key, rel, kind, size)
    jobs.doc_finished(job_id, doc_id, status, error=error)


def _register_and_dispatch(job_id: str, key: str, rel: str, kind: str, size: int) -> None:
    doc_id, _ = jobs.register_doc(job_id, key, rel, kind, size)
    queues.send(config.PROCESS_QUEUE_URL(), {"jobId": job_id, "docId": doc_id})


def file_task(job: dict, msg: dict) -> None:
    job_id, task_id = job["jobId"], msg["taskId"]
    bucket, key, rel, size = config.DATA_BUCKET(), msg["key"], msg["relPath"], int(msg.get("size", 0))
    kind = filetypes.classify(rel, _head(bucket, key))

    if kind == filetypes.ZIP:
        if int(msg.get("depth", 0)) >= config.MAX_ZIP_DEPTH:
            jobs.task_expand(job_id, task_id, 1)
            _register_terminal(job_id, key, rel, kind, size, "skipped", "zip nested too deeply")
        else:
            plan_zip(job, msg)
            return  # plan_zip releases the task
    elif kind == filetypes.UNSUPPORTED:
        jobs.task_expand(job_id, task_id, 1)
        _register_terminal(job_id, key, rel, kind, size, "skipped", "unsupported file type")
    else:
        jobs.task_expand(job_id, task_id, 1)
        _register_and_dispatch(job_id, key, rel, kind, size)
    jobs.task_done(job_id, task_id)


def plan_zip(job: dict, msg: dict) -> None:
    """Read the zip's central directory (ranged reads only) and split it into batches."""
    job_id, task_id = job["jobId"], msg["taskId"]
    bucket, key, rel = config.DATA_BUCKET(), msg["key"], msg["relPath"]
    try:
        with zipfile.ZipFile(s3io.S3SeekableReader(bucket, key)) as zf:
            infos = [i for i in zf.infolist() if not i.is_dir()]
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, EOFError) as exc:
        jobs.task_expand(job_id, task_id, 1)
        _register_terminal(job_id, key, rel, filetypes.ZIP, int(msg.get("size", 0)), "failed", f"bad zip: {exc}")
        jobs.task_done(job_id, task_id)
        return

    batches: list[tuple[int, int]] = []
    start, acc = 0, 0
    for idx, info in enumerate(infos):
        if idx > start and (idx - start >= config.ZIP_BATCH_SIZE or acc + info.compress_size > ZIP_BATCH_BYTES):
            batches.append((start, idx))
            start, acc = idx, 0
        acc += info.compress_size
    if start < len(infos):
        batches.append((start, len(infos)))

    jobs.task_expand(job_id, task_id, len(batches))
    queues.send_many(config.INGEST_QUEUE_URL(), (
        {"type": "zipbatch", "jobId": job_id, "taskId": f"{task_id}-b{i}", "key": key, "relPath": rel,
         "start": s, "end": e, "depth": int(msg.get("depth", 0))}
        for i, (s, e) in enumerate(batches)
    ))
    jobs.task_done(job_id, task_id)


# ---------------------------------------------------------------- zip batches
def zip_batch(job: dict, msg: dict) -> None:
    job_id, task_id = job["jobId"], msg["taskId"]
    bucket, key, zip_rel = config.DATA_BUCKET(), msg["key"], msg["relPath"]
    depth = int(msg.get("depth", 0))
    out_prefix = f"{config.WORK_PREFIX}/{job_id}/extracted/{_h(key)}/"

    planned: list[dict] = []
    with zipfile.ZipFile(s3io.S3SeekableReader(bucket, key)) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()][msg["start"]: msg["end"]]
        for info in infos:
            name = info.filename
            rel = filetypes.safe_relpath(posixpath.join(zip_rel, name))
            kind = filetypes.classify(name)
            entry_key = out_prefix + filetypes.safe_relpath(name)
            if kind == filetypes.UNSUPPORTED:
                planned.append({"action": "skip", "key": entry_key, "rel": rel, "kind": kind, "size": info.file_size,
                                "error": "unsupported file type"})
                continue
            if info.flag_bits & 0x1:
                planned.append({"action": "fail", "key": entry_key, "rel": rel, "kind": kind, "size": info.file_size,
                                "error": "file is password protected inside the zip"})
                continue
            if not s3io.exists(bucket, entry_key):
                try:
                    _extract(zf, info, bucket, entry_key)
                except (zipfile.BadZipFile, NotImplementedError, RuntimeError, OSError, EOFError) as exc:
                    planned.append({"action": "fail", "key": entry_key, "rel": rel, "kind": kind,
                                    "size": info.file_size, "error": f"could not extract: {exc}"})
                    continue
            planned.append({"action": "zip" if kind == filetypes.ZIP else "doc", "key": entry_key, "rel": rel,
                            "kind": kind, "size": info.file_size})

    jobs.task_expand(job_id, task_id, len(planned))
    for p in planned:
        if p["action"] == "doc":
            _register_and_dispatch(job_id, p["key"], p["rel"], p["kind"], p["size"])
        elif p["action"] == "zip":
            queues.send(config.INGEST_QUEUE_URL(), {
                "type": "file", "jobId": job_id, "taskId": f"f-{_h(p['key'])}", "key": p["key"],
                "relPath": p["rel"], "size": p["size"], "depth": depth + 1,
            })
        else:
            _register_terminal(job_id, p["key"], p["rel"], p["kind"], p["size"],
                               "skipped" if p["action"] == "skip" else "failed", p["error"])
    jobs.task_done(job_id, task_id)


def _extract(zf: zipfile.ZipFile, info: zipfile.ZipInfo, bucket: str, key: str) -> None:
    ctype = filetypes.content_type(info.filename)
    if info.file_size <= 64 * 1024 * 1024:
        with zf.open(info) as src:
            s3io.put_bytes(bucket, key, src.read(), ctype)
        return
    writer = s3io.S3MultipartWriter(bucket, key, ctype)
    try:
        with zf.open(info) as src:
            while True:
                chunk = src.read(16 * 1024 * 1024)
                if not chunk:
                    break
                writer.write(chunk)
        writer.close()
    except Exception:
        writer.abort()
        raise
