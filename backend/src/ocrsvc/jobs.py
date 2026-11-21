"""Job / document state machine.

Completion tracking uses a single `pending` counter on the job item. Every unit of
outstanding work (a scan page, a file waiting to be classified, a zip batch, a
document being OCR'd) holds one slot. A task always *adds* slots for the children
it creates before it *releases* its own slot, so the counter can only reach zero
when everything is finished. Both steps are guarded by idempotency markers, so
SQS redeliveries never double count.

Job status:   created -> processing -> finalizing -> done
Doc status:   queued -> processing -> assembling -> done | failed | skipped
"""
from __future__ import annotations

import hashlib
import logging

from . import config, db, queues, settings, users

log = logging.getLogger("ocr")

TERMINAL_DOC = ("done", "failed", "skipped")


# ---------------------------------------------------------------- jobs
def create_job(owner: str, name: str, engine: str, outputs: list[str]) -> dict:
    job_id = db.new_id(12)
    now = db.now_iso()
    item = {
        **db.job_key(job_id),
        "jobId": job_id,
        "owner": owner,
        "name": name[:200] or "Untitled job",
        "engine": engine,
        "outputs": outputs,
        "status": "created",
        "pending": 0,
        "docsTotal": 0, "docsDone": 0, "docsFailed": 0, "docsSkipped": 0,
        "pagesTotal": 0, "pagesDone": 0,
        "creditsCharged": 0,
        "createdAt": now,
        "GSI1PK": f"OWNER#{owner}",
        "GSI1SK": now,
    }
    db.put(item, condition="attribute_not_exists(PK)")
    return item


def get_job(job_id: str, consistent: bool = False) -> dict | None:
    return db.get(db.job_key(job_id), consistent)


def upload_prefix(owner: str, job_id: str) -> str:
    return f"{config.UPLOAD_PREFIX}/{owner}/{job_id}/"


def submit(job_id: str) -> bool:
    """created -> processing, and enqueue the first scan of the upload prefix."""
    new = db.update(
        db.job_key(job_id),
        "SET #s = :p, pending = :one, submittedAt = :t",
        {":p": "processing", ":one": 1, ":t": db.now_iso(), ":c": "created"},
        names={"#s": "status"},
        condition="#s = :c",
    )
    if not new:
        return False
    queues.send(config.INGEST_QUEUE_URL(), {"type": "scan", "jobId": job_id, "taskId": "scan-0"})
    return True


def _marker(job_id: str, name: str) -> dict:
    return {"PK": f"JOB#{job_id}", "SK": f"TASK#{name}", "at": db.now_iso()}


def task_expand(job_id: str, task_id: str, children: int, extra: dict | None = None) -> bool:
    """Reserve `children` pending slots for a task. Returns False if already done before."""
    if children <= 0 and not extra:
        return True
    expr = "ADD pending :n"
    values: dict = {":n": children}
    for i, (k, v) in enumerate((extra or {}).items()):
        expr += f", {k} :x{i}"
        values[f":x{i}"] = v
    try:
        db.transact([
            db.tx_put(_marker(job_id, f"{task_id}#expand"), condition="attribute_not_exists(PK)"),
            db.tx_update(db.job_key(job_id), expr, values),
        ])
        return True
    except db.TxConflict:
        return False


def task_done(job_id: str, task_id: str, extra: dict | None = None) -> None:
    """Release the task's own pending slot (exactly once)."""
    expr = "ADD pending :m"
    values: dict = {":m": -1}
    for i, (k, v) in enumerate((extra or {}).items()):
        expr += f", {k} :x{i}"
        values[f":x{i}"] = v
    try:
        db.transact([
            db.tx_put(_marker(job_id, f"{task_id}#done"), condition="attribute_not_exists(PK)"),
            db.tx_update(db.job_key(job_id), expr, values),
        ])
    except db.TxConflict:
        return
    check_finished(job_id)


def check_finished(job_id: str) -> None:
    job = get_job(job_id, consistent=True)
    if not job or job["status"] != "processing" or job.get("pending", 1) > 0:
        return
    won = db.update(
        db.job_key(job_id), "SET #s = :f", {":f": "finalizing", ":p": "processing", ":z": 0},
        names={"#s": "status"}, condition="#s = :p AND pending <= :z",
    )
    if won:
        queues.send(config.FINALIZE_QUEUE_URL(), {"jobId": job_id})


# ---------------------------------------------------------------- documents
def doc_id_for(job_id: str, src_key: str) -> str:
    return hashlib.sha1(f"{job_id}|{src_key}".encode()).hexdigest()[:20]


def register_doc(job_id: str, src_key: str, rel_path: str, kind: str, size: int) -> tuple[str, bool]:
    """Create the document item (idempotent). Returns (docId, created_now)."""
    doc_id = doc_id_for(job_id, src_key)
    item = {
        **db.doc_key(job_id, doc_id),
        "docId": doc_id,
        "jobId": job_id,
        "srcKey": src_key,
        "relPath": rel_path,
        "kind": kind,
        "size": size,
        "status": "queued",
        "pages": 0,
        "unitsTotal": 0, "unitsDone": 0, "unitsFailed": 0, "failedPages": 0,
        "createdAt": db.now_iso(),
    }
    created = db.put(item, condition="attribute_not_exists(PK)")
    if created:
        db.update(db.job_key(job_id), "ADD docsTotal :one", {":one": 1})
    return doc_id, created


def get_doc(job_id: str, doc_id: str, consistent: bool = False) -> dict | None:
    return db.get(db.doc_key(job_id, doc_id), consistent)


def start_processing(job_id: str, doc_id: str, pages: int, units_total: int) -> bool:
    """queued -> processing (or already processing, on retry)."""
    new = db.update(
        db.doc_key(job_id, doc_id),
        "SET #s = :p, pages = :pg, unitsTotal = :u",
        {":p": "processing", ":pg": pages, ":u": units_total, ":q": "queued"},
        names={"#s": "status"},
        condition="#s IN (:q, :p)",
    )
    if new is None:
        return False
    if new.get("pagesCounted") is None:
        if db.update(db.doc_key(job_id, doc_id), "SET pagesCounted = :t", {":t": True},
                     condition="attribute_not_exists(pagesCounted)"):
            db.update(db.job_key(job_id), "ADD pagesTotal :pg", {":pg": pages})
    return True


def unit_finished(job_id: str, doc_id: str, unit: int, ok: bool, pages: int, error: str = "") -> None:
    field = "unitsDone" if ok else "unitsFailed"
    expr = f"ADD {field} :one"
    values: dict = {":one": 1}
    if not ok:
        expr += ", failedPages :fp SET lastError = :e"
        values.update({":fp": pages, ":e": error[:500]})
    try:
        db.transact([
            db.tx_put(_marker(job_id, f"unit#{doc_id}#{unit}"), condition="attribute_not_exists(PK)"),
            db.tx_update(db.doc_key(job_id, doc_id), expr, values),
        ])
    except db.TxConflict:
        log.info("duplicate unit completion %s/%s/%s ignored", job_id, doc_id, unit)
    doc = get_doc(job_id, doc_id, consistent=True)
    if not doc or doc["status"] != "processing":
        return
    if doc["unitsDone"] + doc["unitsFailed"] < doc["unitsTotal"]:
        return
    won = db.update(
        db.doc_key(job_id, doc_id), "SET #s = :a", {":a": "assembling", ":p": "processing"},
        names={"#s": "status"}, condition="#s = :p",
    )
    if won:
        queues.send(config.ASSEMBLE_QUEUE_URL(), {"jobId": job_id, "docId": doc_id})


def doc_finished(job_id: str, doc_id: str, status: str, pages_done: int = 0, error: str = "",
                 outputs: dict | None = None, credits: int = 0) -> None:
    """Move a document to a terminal state and release its pending slot (exactly once)."""
    assert status in TERMINAL_DOC
    counter = {"done": "docsDone", "failed": "docsFailed", "skipped": "docsSkipped"}[status]
    doc_expr = "SET #s = :st, finishedAt = :t"
    doc_values: dict = {":st": status, ":t": db.now_iso(), ":d": "done", ":f": "failed", ":k": "skipped"}
    if error:
        doc_expr += ", #e = :e"
        doc_values[":e"] = error[:1000]
    if outputs is not None:
        doc_expr += ", outputs = :o"
        doc_values[":o"] = outputs
    names = {"#s": "status"}
    if error:
        names["#e"] = "error"
    try:
        db.transact([
            db.tx_update(db.doc_key(job_id, doc_id), doc_expr, doc_values, names=names,
                         condition="NOT (#s IN (:d, :f, :k))"),
            db.tx_update(db.job_key(job_id), f"ADD pending :m, {counter} :one, pagesDone :pg, creditsCharged :c",
                         {":m": -1, ":one": 1, ":pg": pages_done, ":c": credits}),
        ])
    except db.TxConflict:
        log.info("doc %s/%s already terminal", job_id, doc_id)
        return
    check_finished(job_id)


# ---------------------------------------------------------------- credits
def reserve_credits(job: dict, doc_id: str, pages: int) -> bool:
    """Reserve credits for a document before OCR starts. True if OK (or already reserved)."""
    profile = users.get_profile(job["owner"]) or {}
    cost = pages * settings.price_per_page(job["engine"])
    if not users.billing_applies(profile) or cost == 0:
        db.update(db.doc_key(job["jobId"], doc_id), "SET reserved = if_not_exists(reserved, :z)", {":z": 0})
        return True
    try:
        db.transact([
            db.tx_update(db.user_key(job["owner"]), "ADD credits :neg", {":neg": -cost, ":cost": cost},
                         condition="credits >= :cost"),
            db.tx_update(db.doc_key(job["jobId"], doc_id), "SET reserved = :cost", {":cost": cost},
                         condition="attribute_not_exists(reserved)"),
            db.tx_put(users.ledger_item(job["owner"], -cost, f"OCR {pages} pages", f"{job['jobId']}/{doc_id}")),
        ])
        return True
    except db.TxConflict as exc:
        if len(exc.reasons) > 1 and exc.reasons[1] == "ConditionalCheckFailed":
            return True  # reserved on an earlier attempt
        return False


def refund_failed_pages(job: dict, doc: dict) -> int:
    """Give back credits for pages that could not be OCR'd. Returns credits actually kept."""
    reserved = int(doc.get("reserved", 0))
    if reserved <= 0:
        return 0
    price = settings.price_per_page(job["engine"])
    refund = min(reserved, int(doc.get("failedPages", 0)) * price)
    if doc.get("status") == "failed":
        refund = reserved
    if refund > 0:
        marked = db.update(db.doc_key(job["jobId"], doc["docId"]), "SET refunded = :r", {":r": refund},
                           condition="attribute_not_exists(refunded)")
        if marked:
            users.grant_credits(job["owner"], refund, "refund for failed pages", f"{job['jobId']}/{doc['docId']}")
    return reserved - refund
