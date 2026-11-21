"""HTTP API (behind CloudFront at /api/*, Cognito JWT authorizer on everything except
/api/public/* and /api/webhooks/*)."""
from __future__ import annotations

import base64
import json
import logging
import math

from .. import aws, config, db, filetypes, jobs, queues, razorpay, s3io, settings, users
from ..http import HttpError, Request, Router, response

log = logging.getLogger("ocr")
log.setLevel(logging.INFO)

router = Router("/api")

SINGLE_PUT_MAX = 64 * 1024 * 1024
MIN_PART = 16 * 1024 * 1024
MAX_FILES_PER_CALL = 200
URL_TTL = 6 * 3600
OUTPUT_FORMATS = ("txt", "json", "pdf")
# Languages installed in the Tesseract image (see backend/tesseract/Dockerfile)
TESSERACT_LANGUAGES = {"eng", "hin", "mar", "guj", "ben", "tam", "tel", "kan", "mal", "pan", "urd", "san",
                       "eng+hin", "eng+mar", "eng+guj", "hin+mar"}


# ---------------------------------------------------------------- helpers
def _cursor_out(key: dict | None) -> str | None:
    return base64.urlsafe_b64encode(json.dumps(key).encode()).decode() if key else None


def _cursor_in(value: str | None) -> dict | None:
    if not value:
        return None
    try:
        return json.loads(base64.urlsafe_b64decode(value.encode()))
    except Exception as exc:  # noqa: BLE001
        raise HttpError(400, "Bad cursor") from exc


def _is_admin(req: Request) -> bool:
    return bool(req.user and req.user.get("role") == "superadmin")


def _require_admin(req: Request) -> None:
    if not _is_admin(req):
        raise HttpError(403, "Superadmin only")


def _own_job(req: Request, job_id: str) -> dict:
    job = jobs.get_job(job_id)
    if not job or job.get("deleted") or (job["owner"] != req.sub and not _is_admin(req)):
        raise HttpError(404, "Job not found")
    return job


def _presign(method: str, params: dict, ttl: int = URL_TTL) -> str:
    return aws.client("s3").generate_presigned_url(method, Params=params, ExpiresIn=ttl)


def _public_job(job: dict) -> dict:
    keys = ("jobId", "name", "engine", "outputs", "status", "docsTotal", "docsDone", "docsFailed", "docsSkipped",
            "pagesTotal", "pagesDone", "creditsCharged", "createdAt", "submittedAt", "finishedAt", "owner",
            "resultBytes", "pending", "language", "lastError")
    out = {k: job.get(k) for k in keys if k in job}
    out["hasResult"] = bool(job.get("resultKey"))
    return out


def _public_user(u: dict) -> dict:
    return {k: u.get(k) for k in ("sub", "email", "role", "status", "credits", "unlimited", "createdAt", "note")}


# ---------------------------------------------------------------- public
@router.route("GET", "/public/config", public=True)
def public_config(req: Request):
    s = settings.get()
    return {
        "siteName": s["siteName"],
        "accessMode": s["accessMode"],
        "billingEnabled": s["billingEnabled"],
        "enabledEngines": s["enabledEngines"],
        "defaultEngine": s["defaultEngine"],
        "prices": s["prices"],
        "packs": s["packs"] if s["billingEnabled"] else [],
        "currency": s["currency"],
        "razorpayKeyId": razorpay.key_id() if s["billingEnabled"] and razorpay.configured() else "",
        "maxFileBytes": s["maxFileBytes"],
    }


@router.route("POST", "/webhooks/razorpay", public=True)
def razorpay_webhook(req: Request):
    if not razorpay.verify_webhook_signature(req.raw_body, req.headers.get("x-razorpay-signature", "")):
        raise HttpError(401, "Bad signature")
    payload = req.json()
    event = payload.get("event", "")
    if event in ("payment.captured", "order.paid"):
        payment = payload.get("payload", {}).get("payment", {}).get("entity", {})
        order_id = payment.get("order_id")
        if order_id:
            razorpay.fulfil(order_id, payment.get("id", ""))
    return {"ok": True}


# ---------------------------------------------------------------- me
@router.route("GET", "/me", allow_inactive=True)
def me(req: Request):
    return {"user": _public_user(req.user or {}), "settings": public_config(req)}


# ---------------------------------------------------------------- jobs
@router.route("GET", "/jobs")
def list_jobs(req: Request):
    owner = req.sub
    if _is_admin(req) and req.query.get("owner"):
        owner = req.query["owner"]
    items, cursor = db.query(f"OWNER#{owner}", index="GSI1", limit=min(int(req.query.get("limit", 25)), 100),
                             cursor=_cursor_in(req.query.get("cursor")), forward=False)
    return {"jobs": [_public_job(j) for j in items if not j.get("deleted")], "cursor": _cursor_out(cursor)}


@router.route("POST", "/jobs")
def create_job(req: Request):
    body = req.json()
    s = settings.get()
    engine = body.get("engine") or s["defaultEngine"]
    if engine not in s["enabledEngines"]:
        raise HttpError(400, f"Engine must be one of {s['enabledEngines']}")
    outputs = [o for o in body.get("outputs", ["txt", "json"]) if o in OUTPUT_FORMATS] or ["txt"]
    if users.billing_applies(req.user or {}) and int((req.user or {}).get("credits", 0)) <= 0:
        raise HttpError(402, "You have no credits left. Buy a credit pack to continue.")
    language = str(body.get("language") or "eng")
    if language not in TESSERACT_LANGUAGES:
        raise HttpError(400, f"language must be one of {sorted(TESSERACT_LANGUAGES)}")
    if engine == "textract":
        language = "eng"  # Textract detects Latin-script languages automatically
        outputs = [o for o in outputs if o != "pdf"] or ["txt"]
    job = jobs.create_job(req.sub, str(body.get("name", "")).strip(), engine, outputs)
    if language != "eng":
        db.update(db.job_key(job["jobId"]), "SET #l = :l", {":l": language}, names={"#l": "language"})
        job["language"] = language
    return response(201, {"job": _public_job(job)})


@router.route("GET", "/jobs/{job_id}")
def get_job(req: Request):
    return {"job": _public_job(_own_job(req, req.params["job_id"]))}


@router.route("DELETE", "/jobs/{job_id}")
def delete_job(req: Request):
    job = _own_job(req, req.params["job_id"])
    if job["status"] in ("processing", "finalizing"):
        raise HttpError(409, "Wait for the job to finish before deleting it")
    db.update(db.job_key(job["jobId"]), "SET deleted = :t", {":t": True})
    queues.send(config.FINALIZE_QUEUE_URL(), {"type": "delete", "jobId": job["jobId"]})
    return {"ok": True}


@router.route("POST", "/jobs/{job_id}/uploads")
def start_uploads(req: Request):
    job = _own_job(req, req.params["job_id"])
    if job["status"] != "created":
        raise HttpError(409, "Uploads are closed for this job")
    files = req.json().get("files") or []
    if not isinstance(files, list) or not files or len(files) > MAX_FILES_PER_CALL:
        raise HttpError(400, f"Send between 1 and {MAX_FILES_PER_CALL} files per call")
    max_bytes = settings.get()["maxFileBytes"]
    bucket = config.DATA_BUCKET()
    prefix = jobs.upload_prefix(job["owner"], job["jobId"])
    s3 = aws.client("s3")
    out = []
    for f in files:
        rel = filetypes.safe_relpath(str(f.get("path") or f.get("name") or "file"))
        size = int(f.get("size") or 0)
        if size <= 0:
            out.append({"path": rel, "error": "empty file"})
            continue
        if size > max_bytes:
            out.append({"path": rel, "error": f"file is larger than {max_bytes} bytes"})
            continue
        if filetypes.classify(rel) == filetypes.UNSUPPORTED:
            out.append({"path": rel, "error": "unsupported file type", "skipped": True})
            continue
        key = prefix + rel
        ctype = filetypes.content_type(rel)
        if size <= SINGLE_PUT_MAX:
            url = _presign("put_object", {"Bucket": bucket, "Key": key, "ContentType": ctype})
            out.append({"path": rel, "key": key, "method": "single", "url": url, "contentType": ctype})
        else:
            part_size = max(MIN_PART, math.ceil(size / 9000))
            n_parts = math.ceil(size / part_size)
            upload_id = s3.create_multipart_upload(Bucket=bucket, Key=key, ContentType=ctype)["UploadId"]
            urls = [
                _presign("upload_part", {"Bucket": bucket, "Key": key, "UploadId": upload_id, "PartNumber": i})
                for i in range(1, n_parts + 1)
            ]
            out.append({"path": rel, "key": key, "method": "multipart", "uploadId": upload_id,
                        "partSize": part_size, "partUrls": urls})
    return {"uploads": out}


def _check_key(job: dict, key: str) -> None:
    if not key.startswith(jobs.upload_prefix(job["owner"], job["jobId"])):
        raise HttpError(400, "Key does not belong to this job")


@router.route("POST", "/jobs/{job_id}/uploads/complete")
def complete_upload(req: Request):
    job = _own_job(req, req.params["job_id"])
    body = req.json()
    key, upload_id, parts = body.get("key", ""), body.get("uploadId", ""), body.get("parts") or []
    _check_key(job, key)
    parts = sorted(({"PartNumber": int(p["PartNumber"]), "ETag": str(p["ETag"])} for p in parts),
                   key=lambda p: p["PartNumber"])
    aws.client("s3").complete_multipart_upload(Bucket=config.DATA_BUCKET(), Key=key, UploadId=upload_id,
                                               MultipartUpload={"Parts": parts})
    return {"ok": True}


@router.route("POST", "/jobs/{job_id}/uploads/abort")
def abort_upload(req: Request):
    job = _own_job(req, req.params["job_id"])
    body = req.json()
    _check_key(job, body.get("key", ""))
    aws.client("s3").abort_multipart_upload(Bucket=config.DATA_BUCKET(), Key=body["key"], UploadId=body["uploadId"])
    return {"ok": True}


@router.route("POST", "/jobs/{job_id}/submit")
def submit_job(req: Request):
    job = _own_job(req, req.params["job_id"])
    if job["status"] != "created":
        raise HttpError(409, "Job already submitted")
    contents, _ = s3io.list_keys(config.DATA_BUCKET(), jobs.upload_prefix(job["owner"], job["jobId"]), max_keys=1)
    if not contents:
        raise HttpError(400, "Upload at least one file first")
    if not jobs.submit(job["jobId"]):
        raise HttpError(409, "Job already submitted")
    return {"job": _public_job(jobs.get_job(job["jobId"], consistent=True))}


@router.route("GET", "/jobs/{job_id}/documents")
def list_documents(req: Request):
    job = _own_job(req, req.params["job_id"])
    items, cursor = db.query(f"JOB#{job['jobId']}", "DOC#", limit=min(int(req.query.get("limit", 100)), 500),
                             cursor=_cursor_in(req.query.get("cursor")))
    status = req.query.get("status")
    docs = [
        {k: d.get(k) for k in ("docId", "relPath", "kind", "size", "status", "pages", "unitsTotal", "unitsDone",
                                "unitsFailed", "error", "outputs", "reserved", "refunded")}
        for d in items if not status or d.get("status") == status
    ]
    return {"documents": docs, "cursor": _cursor_out(cursor)}


@router.route("GET", "/jobs/{job_id}/documents/{doc_id}/download")
def download_document(req: Request):
    job = _own_job(req, req.params["job_id"])
    doc = jobs.get_doc(job["jobId"], req.params["doc_id"])
    fmt = req.query.get("format", "txt")
    if not doc or not (doc.get("outputs") or {}).get(fmt):
        raise HttpError(404, "Output not available")
    key = doc["outputs"][fmt]
    name = key.rsplit("/", 1)[-1].replace('"', "")
    return {"url": _presign("get_object", {"Bucket": config.DATA_BUCKET(), "Key": key,
                                           "ResponseContentDisposition": f'attachment; filename="{name}"'}, 3600)}


@router.route("GET", "/jobs/{job_id}/download")
def download_job(req: Request):
    job = _own_job(req, req.params["job_id"])
    if not job.get("resultKey"):
        raise HttpError(404, "Results are not ready yet")
    name = f"{job['name'] or job['jobId']}-ocr.zip".replace('"', "").replace("/", "_")
    return {"url": _presign("get_object", {"Bucket": config.DATA_BUCKET(), "Key": job["resultKey"],
                                           "ResponseContentDisposition": f'attachment; filename="{name}"'}, 3600)}


# ---------------------------------------------------------------- billing
@router.route("GET", "/billing/ledger")
def ledger(req: Request):
    items, cursor = db.query(f"USER#{req.sub}", "LEDGER#", limit=50, cursor=_cursor_in(req.query.get("cursor")),
                             forward=False)
    return {"entries": [{k: i.get(k) for k in ("delta", "reason", "ref", "at")} for i in items],
            "cursor": _cursor_out(cursor)}


@router.route("POST", "/billing/orders")
def create_order(req: Request):
    s = settings.get()
    if not s["billingEnabled"]:
        raise HttpError(400, "Billing is disabled")
    if not razorpay.configured():
        raise HttpError(503, "Payments are not configured yet")
    pack_id = req.json().get("packId")
    pack = next((p for p in s["packs"] if p["id"] == pack_id), None)
    if not pack:
        raise HttpError(400, "Unknown pack")
    order = razorpay.create_order(req.sub, pack, s["currency"])
    return {"orderId": order["id"], "amount": order["amount"], "currency": order["currency"],
            "keyId": razorpay.key_id(), "pack": pack, "email": req.user.get("email") if req.user else ""}


@router.route("POST", "/billing/verify")
def verify_payment(req: Request):
    body = req.json()
    order_id, payment_id = body.get("razorpay_order_id", ""), body.get("razorpay_payment_id", "")
    if not razorpay.verify_payment_signature(order_id, payment_id, body.get("razorpay_signature", "")):
        raise HttpError(400, "Payment signature is invalid")
    order = db.get({"PK": f"ORDER#{order_id}", "SK": "ORDER"})
    if not order or order["sub"] != req.sub:
        raise HttpError(404, "Order not found")
    razorpay.fulfil(order_id, payment_id)
    return {"user": _public_user(users.get_profile(req.sub) or {})}


# ---------------------------------------------------------------- admin
@router.route("GET", "/admin/settings")
def admin_get_settings(req: Request):
    _require_admin(req)
    return {"settings": settings.get(fresh=True), "razorpayConfigured": razorpay.configured()}


@router.route("PUT", "/admin/settings")
def admin_put_settings(req: Request):
    _require_admin(req)
    try:
        return {"settings": settings.save(req.json())}
    except settings.SettingsError as exc:
        raise HttpError(400, str(exc)) from exc


@router.route("GET", "/admin/users")
def admin_users(req: Request):
    _require_admin(req)
    items, cursor = db.query("USERS", index="GSI1", limit=min(int(req.query.get("limit", 50)), 200),
                             cursor=_cursor_in(req.query.get("cursor")), forward=False)
    status = req.query.get("status")
    return {"users": [_public_user(u) for u in items if not status or u.get("status") == status],
            "cursor": _cursor_out(cursor)}


@router.route("PATCH", "/admin/users/{sub}")
def admin_update_user(req: Request):
    _require_admin(req)
    sub = req.params["sub"]
    target = users.get_profile(sub)
    if not target:
        raise HttpError(404, "User not found")
    body = req.json()
    sets, values, names = [], {}, {}
    if "status" in body:
        if body["status"] not in users.STATUSES:
            raise HttpError(400, f"status must be one of {users.STATUSES}")
        if target.get("role") == "superadmin" and body["status"] != "active":
            raise HttpError(400, "The superadmin cannot be suspended")
        sets.append("#s = :s")
        values[":s"], names["#s"] = body["status"], "status"
        _set_cognito_enabled(target, body["status"] != "banned")
    if "unlimited" in body:
        sets.append("unlimited = :u")
        values[":u"] = bool(body["unlimited"])
    if "note" in body:
        sets.append("note = :n")
        values[":n"] = str(body["note"])[:500]
    if sets:
        db.update(db.user_key(sub), "SET " + ", ".join(sets), values, names or None)
    delta = int(body.get("creditsDelta") or 0)
    if delta:
        if users.grant_credits(sub, delta, str(body.get("reason") or "admin adjustment"), req.sub or "") is None:
            raise HttpError(400, "User does not have that many credits")
    return {"user": _public_user(users.get_profile(sub) or {})}


def _set_cognito_enabled(target: dict, enabled: bool) -> None:
    pool = config.env("USER_POOL_ID", "")
    if not pool:
        return
    idp = aws.client("cognito-idp")
    try:
        if enabled:
            idp.admin_enable_user(UserPoolId=pool, Username=target["sub"])
        else:
            idp.admin_disable_user(UserPoolId=pool, Username=target["sub"])
            idp.admin_user_global_sign_out(UserPoolId=pool, Username=target["sub"])
    except Exception as exc:  # noqa: BLE001 - profile status still blocks API access
        log.warning("could not change Cognito state for %s: %s", target["sub"], exc)


@router.route("GET", "/admin/invites")
def admin_invites(req: Request):
    _require_admin(req)
    items, cursor = db.query("INVITES", index="GSI1", limit=200, cursor=_cursor_in(req.query.get("cursor")),
                             forward=False)
    return {"invites": [{k: i.get(k) for k in ("email", "credits", "createdAt", "usedAt")} for i in items],
            "cursor": _cursor_out(cursor)}


@router.route("POST", "/admin/invites")
def admin_create_invites(req: Request):
    _require_admin(req)
    body = req.json()
    emails = body.get("emails") or ([body["email"]] if body.get("email") else [])
    credits = int(body.get("credits", settings.get()["freeCreditsOnSignup"]))
    created = []
    for raw in emails[:500]:
        email = str(raw).strip().lower()
        if "@" not in email or len(email) > 254:
            continue
        now = db.now_iso()
        db.put({**db.invite_key(email), "email": email, "credits": credits, "createdAt": now,
                "invitedBy": req.sub, "GSI1PK": "INVITES", "GSI1SK": now})
        created.append(email)
    return response(201, {"invited": created})


@router.route("DELETE", "/admin/invites/{email}")
def admin_delete_invite(req: Request):
    _require_admin(req)
    from urllib.parse import unquote

    db.table().delete_item(Key=db.invite_key(unquote(req.params["email"])))
    return {"ok": True}


@router.route("POST", "/admin/jobs/{job_id}/finalize")
def admin_force_finalize(req: Request):
    """Safety valve: finish a job that is stuck (e.g. after an AWS outage) with whatever is done."""
    _require_admin(req)
    job = jobs.get_job(req.params["job_id"], consistent=True)
    if not job or job["status"] != "processing":
        raise HttpError(409, "Only processing jobs can be force-finalized")
    db.update(db.job_key(job["jobId"]), "SET pending = :z, lastError = :e",
              {":z": 0, ":e": f"force-finalized by superadmin at {db.now_iso()}"})
    jobs.check_finished(job["jobId"])
    return {"job": _public_job(jobs.get_job(job["jobId"], consistent=True))}


# ---------------------------------------------------------------- entry point
def handler(event, context):
    try:
        method = event["requestContext"]["http"]["method"]
        fn, params, opts = router.match(method, event.get("rawPath", "/"))
        req = Request(event, params)
        if not opts.get("public"):
            if not req.sub:
                raise HttpError(401, "Sign in first")
            req.user = users.get_profile(req.sub) or (users.create_profile(req.sub, req.email) if req.email else None)
            if not req.user:
                raise HttpError(403, "No profile for this account")
            if not opts.get("allow_inactive") and req.user.get("status") != "active":
                raise HttpError(403, "Your account is waiting for approval" if req.user.get("status") == "pending"
                                else "Your account is suspended", code=f"account_{req.user.get('status')}")
        result = fn(req)
        if isinstance(result, dict) and "statusCode" in result:
            return result
        return response(200, result)
    except HttpError as exc:
        return response(exc.status, {"error": exc.message, "code": exc.code})
    except Exception:  # noqa: BLE001
        log.exception("unhandled error")
        return response(500, {"error": "Internal error", "code": "internal"})
