"""Self-hosted runner for the OCR site: the same Lambda handlers, without AWS.

Runs, in one container:
  * an HTTP server: the React app, a generated /config.json, and /api/* translated into API
    Gateway (HTTP API v2) events for ocrsvc.handlers.api. Users are authenticated by Cloudflare
    Access; every API call must carry a valid Access JWT (verified here, not just trusted).
  * queue workers: one OS process per concurrent consumer, polling the SQS-compatible queues
    (ElasticMQ) and calling the pipeline handlers with SQS-shaped events, exactly like Lambda's
    event source mapping (ReportBatchItemFailures, redrive to DLQs after N receives).
  * the sweeper every 15 minutes (EventBridge Scheduler on AWS).

Storage is DynamoDB Local + an S3-compatible server, reached through AWS_ENDPOINT_URL_<SERVICE>.
Textract does not exist outside AWS: the Tesseract engine is the only one offered.
"""
from __future__ import annotations

import base64
import importlib
import json
import logging
import mimetypes
import multiprocessing as mp
import os
import signal
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(processName)s %(levelname)s %(message)s")
log = logging.getLogger("selfhost")

PREFIX = os.environ.get("RESOURCE_PREFIX", "ocr")
WEB_DIR = Path(os.environ.get("WEB_DIR", "/app/web")).resolve()
PORT = int(os.environ.get("PORT", "8080"))
ACCESS_TEAM = os.environ.get("ACCESS_TEAM_DOMAIN", "").strip()  # e.g. myteam.cloudflareaccess.com
ACCESS_AUD = os.environ.get("ACCESS_AUD", "").strip()
ACCESS_CERTS_URL = os.environ.get("ACCESS_CERTS_URL") or (f"https://{ACCESS_TEAM}/cdn-cgi/access/certs" if ACCESS_TEAM else "")
ACCESS_ISSUER = os.environ.get("ACCESS_ISSUER") or (f"https://{ACCESS_TEAM}" if ACCESS_TEAM else "")
TESSERACT_WORKERS = int(os.environ.get("TESSERACT_WORKERS") or max(1, (os.cpu_count() or 2) // 2))

# queue -> (handler, batch size, processes)
CONSUMERS = {
    "ingest": ("ocrsvc.handlers.ingest.handler", 5, 2),
    "process": ("ocrsvc.handlers.process.handler", 1, 2),
    "textract": ("__main__.textract_unavailable", 10, 1),
    "tesseract": ("ocrsvc.handlers.tesseract.handler", 1, TESSERACT_WORKERS),
    "assemble": ("ocrsvc.handlers.assemble.handler", 1, 2),
    "finalize": ("ocrsvc.handlers.finalize.handler", 1, 1),
}
DLQ_CONSUMER = ("ocrsvc.handlers.dlq.handler", 10)
ALL_QUEUES = ["ingest", "process", "textract", "textract-done", "tesseract", "assemble", "finalize"]


# ---------------------------------------------------------------- bootstrap
def wait_for(name: str, fn, timeout: int = 120) -> None:
    deadline = time.time() + timeout
    while True:
        try:
            fn()
            return
        except Exception as exc:  # noqa: BLE001
            if time.time() > deadline:
                raise RuntimeError(f"{name} did not come up: {exc}") from exc
            time.sleep(2)


def bootstrap() -> None:
    import boto3

    ddb, s3, sqs = boto3.client("dynamodb"), boto3.client("s3"), boto3.client("sqs")
    table, bucket = os.environ["TABLE_NAME"], os.environ["DATA_BUCKET"]

    wait_for("DynamoDB", ddb.list_tables)
    if table not in ddb.list_tables()["TableNames"]:
        log.info("creating table %s", table)
        attrs = ["PK", "SK", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK"]
        gsi = [{"IndexName": n, "Projection": {"ProjectionType": "ALL"},
                "KeySchema": [{"AttributeName": f"{n}PK", "KeyType": "HASH"},
                              {"AttributeName": f"{n}SK", "KeyType": "RANGE"}]} for n in ("GSI1", "GSI2")]
        ddb.create_table(TableName=table, BillingMode="PAY_PER_REQUEST",
                         AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in attrs],
                         KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"},
                                    {"AttributeName": "SK", "KeyType": "RANGE"}],
                         GlobalSecondaryIndexes=gsi)

    wait_for("S3", s3.list_buckets)
    if bucket not in [b["Name"] for b in s3.list_buckets().get("Buckets", [])]:
        log.info("creating bucket %s", bucket)
        s3.create_bucket(Bucket=bucket)
    lifecycle = [
        {"ID": "uploads", "Status": "Enabled", "Filter": {"Prefix": "uploads/"},
         "Expiration": {"Days": int(os.environ.get("UPLOAD_RETENTION_DAYS", "7"))}},
        {"ID": "work", "Status": "Enabled", "Filter": {"Prefix": "work/"}, "Expiration": {"Days": 3}},
        {"ID": "results", "Status": "Enabled", "Filter": {"Prefix": "results/"},
         "Expiration": {"Days": int(os.environ.get("RESULT_RETENTION_DAYS", "30"))}},
    ]
    try:
        s3.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration={"Rules": lifecycle})
    except Exception as exc:  # noqa: BLE001 - not every S3-compatible server supports lifecycle rules
        log.warning("bucket lifecycle not applied: %s", exc)
    try:
        s3.put_bucket_cors(Bucket=bucket, CORSConfiguration={"CORSRules": [{
            "AllowedMethods": ["PUT", "GET", "HEAD"], "AllowedOrigins": [os.environ.get("SITE_ORIGIN", "*")],
            "AllowedHeaders": ["*"], "ExposeHeaders": ["ETag"], "MaxAgeSeconds": 3600}]})
    except Exception as exc:  # noqa: BLE001
        log.warning("bucket CORS not applied (server default applies): %s", exc)

    wait_for("SQS", sqs.list_queues)
    for q in ALL_QUEUES:
        url = sqs.get_queue_url(QueueName=f"{PREFIX}-{q}")["QueueUrl"]
        os.environ[f"{q.replace('-', '_').upper()}_QUEUE_URL"] = url

    # Textract is AWS-only: offer Tesseract alone.
    sys.path.insert(0, "/app")
    from ocrsvc import db

    current = db.get(db.SETTINGS_KEY) or {}
    engines = [e for e in current.get("enabledEngines", ["tesseract"]) if e != "textract"] or ["tesseract"]
    db.update(db.SETTINGS_KEY, "SET enabledEngines = :e, defaultEngine = :d",
              {":e": engines, ":d": "tesseract"})
    log.info("bootstrap complete")


def textract_unavailable(event, context):
    """Consumer for the textract queue: fail those units cleanly so jobs still finish."""
    from ocrsvc.handlers import dlq

    for rec in event.get("Records", []):
        body = json.loads(rec["body"])
        if "unit" in body:
            from ocrsvc import jobs
            jobs.unit_finished(body["jobId"], body["docId"], int(body["unit"]), False, int(body.get("pages", 0)),
                               "Textract is not available on this server; use the Tesseract engine")
        else:
            dlq.handle(rec.get("eventSourceARN", ""), body)
    return {"batchItemFailures": []}


# ---------------------------------------------------------------- queue workers
def worker(queue: str, handler_path: str, batch: int, idx: int, env: dict) -> None:
    os.environ.update(env)
    tmp = Path(f"/tmp/w-{queue}-{idx}")
    tmp.mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = str(tmp)  # handlers clean their TMPDIR at start; never share it
    tempfile.tempdir = None
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    import boto3

    mod_name, fn_name = handler_path.rsplit(".", 1)
    handler = getattr(sys.modules[__name__] if mod_name == "__main__" else importlib.import_module(mod_name), fn_name)
    sqs = boto3.client("sqs")
    url = os.environ[f"{queue.replace('-', '_').upper()}_QUEUE_URL"]
    arn = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    run_queue(sqs, url, arn, handler, batch)


def dlq_worker(env: dict) -> None:
    os.environ.update(env)
    import boto3

    handler_path, batch = DLQ_CONSUMER
    mod_name, fn_name = handler_path.rsplit(".", 1)
    handler = getattr(importlib.import_module(mod_name), fn_name)
    sqs = boto3.client("sqs")
    dlqs = []
    for q in ALL_QUEUES:
        url = sqs.get_queue_url(QueueName=f"{PREFIX}-{q}-dlq")["QueueUrl"]
        arn = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
        dlqs.append((url, arn))
    while True:
        busy = False
        for url, arn in dlqs:
            busy |= poll_once(sqs, url, arn, handler, batch, wait=1)
        if not busy:
            time.sleep(5)


def run_queue(sqs, url: str, arn: str, handler, batch: int) -> None:
    while True:
        try:
            poll_once(sqs, url, arn, handler, batch, wait=20)
        except Exception:  # noqa: BLE001 - keep polling (e.g. queue service restarting)
            log.exception("polling %s failed", url)
            time.sleep(5)


def poll_once(sqs, url: str, arn: str, handler, batch: int, wait: int) -> bool:
    msgs = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=batch, WaitTimeSeconds=wait,
                               AttributeNames=["All"]).get("Messages", [])
    if not msgs:
        return False
    event = {"Records": [{
        "messageId": m["MessageId"], "receiptHandle": m["ReceiptHandle"], "body": m["Body"],
        "attributes": m.get("Attributes", {}), "eventSource": "aws:sqs", "eventSourceARN": arn,
    } for m in msgs]}
    try:
        result = handler(event, None) or {}
    except Exception:  # noqa: BLE001 - like Lambda: the whole batch becomes visible again later
        log.exception("handler failed for %d message(s) from %s", len(msgs), url)
        return True
    failed = {f["itemIdentifier"] for f in result.get("batchItemFailures", [])}
    done = [m for m in msgs if m["MessageId"] not in failed]
    for i in range(0, len(done), 10):
        sqs.delete_message_batch(QueueUrl=url, Entries=[
            {"Id": str(n), "ReceiptHandle": m["ReceiptHandle"]} for n, m in enumerate(done[i:i + 10])])
    return True


def sweeper_loop(env: dict) -> None:
    os.environ.update(env)
    from ocrsvc.handlers import sweeper

    while True:
        time.sleep(900)
        try:
            sweeper.handler({}, None)
        except Exception:  # noqa: BLE001
            log.exception("sweep failed")


# ---------------------------------------------------------------- HTTP
_jwks = None


def verify_access_jwt(token: str) -> dict:
    global _jwks
    import jwt

    if _jwks is None:
        _jwks = jwt.PyJWKClient(ACCESS_CERTS_URL, cache_keys=True, lifespan=3600)
    key = _jwks.get_signing_key_from_jwt(token).key
    return jwt.decode(token, key, algorithms=["RS256"], audience=ACCESS_AUD, issuer=ACCESS_ISSUER,
                      options={"require": ["exp", "iat", "sub", "email"]})


SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ocr"

    def log_message(self, fmt, *args):  # quieter access log
        log.info("%s %s", self.command, self.path.split("?", 1)[0])

    def do_GET(self):
        self.route()

    def do_HEAD(self):
        self.route()

    def do_POST(self):
        self.route()

    def do_PUT(self):
        self.route()

    def do_PATCH(self):
        self.route()

    def do_DELETE(self):
        self.route()

    def send(self, status: int, body: bytes, headers: dict) -> None:
        self.send_response(status)
        for k, v in {**SECURITY_HEADERS, **headers}.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status: int, data) -> None:
        self.send(status, json.dumps(data).encode(), {"Content-Type": "application/json", "Cache-Control": "no-store"})

    def route(self) -> None:
        path, _, query = self.path.partition("?")
        try:
            if path.startswith("/api/"):
                self.api(path, query)
            elif path == "/config.json":
                self.send_json(200, {"region": os.environ.get("AWS_REGION", "ap-south-1"), "apiBase": "/api",
                                     "auth": "cloudflare-access", "userPoolId": "", "userPoolClientId": ""})
            elif path == "/healthz":
                self.send_json(200, {"ok": True})
            else:
                self.static(path)
        except Exception:  # noqa: BLE001
            log.exception("request failed")
            self.send_json(500, {"error": "Internal error", "code": "internal"})

    def api(self, path: str, query: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 10 * 1024 * 1024:
            return self.send_json(413, {"error": "Request too large"})
        raw = self.rfile.read(length) if length else b""

        public = path.startswith("/api/public/") or path.startswith("/api/webhooks/")
        claims = None
        if not public:
            if not (ACCESS_CERTS_URL and ACCESS_AUD):
                return self.send_json(503, {"error": "Sign-in is not configured on this server", "code": "no_auth"})
            token = self.headers.get("Cf-Access-Jwt-Assertion") or self.cookie("CF_Authorization")
            if not token:
                return self.send_json(401, {"error": "Sign in first", "code": "unauthenticated"})
            try:
                c = verify_access_jwt(token)
            except Exception as exc:  # noqa: BLE001
                log.warning("rejected Access token: %s", exc)
                return self.send_json(401, {"error": "Your sign-in is invalid or expired", "code": "unauthenticated"})
            claims = {"sub": f"cf-{c['sub']}", "email": c["email"].lower()}

        try:
            body, b64 = raw.decode("utf-8"), False
        except UnicodeDecodeError:
            body, b64 = base64.b64encode(raw).decode(), True
        params = {k: v[-1] for k, v in urllib.parse.parse_qs(query).items()}
        event = {
            "version": "2.0",
            "rawPath": path,
            "rawQueryString": query,
            "queryStringParameters": params or None,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": body or None,
            "isBase64Encoded": b64,
            "requestContext": {"http": {"method": self.command, "path": path}},
        }
        if claims:
            event["requestContext"]["authorizer"] = {"jwt": {"claims": claims}}

        from ocrsvc.handlers import api as api_handler

        resp = api_handler.handler(event, None)
        out = resp.get("body") or ""
        data = base64.b64decode(out) if resp.get("isBase64Encoded") else out.encode()
        self.send(int(resp.get("statusCode", 200)), data, resp.get("headers") or {})

    def cookie(self, name: str) -> str | None:
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return None

    def static(self, path: str) -> None:
        rel = urllib.parse.unquote(path).lstrip("/") or "index.html"
        target = (WEB_DIR / rel).resolve()
        if not target.is_relative_to(WEB_DIR) or not target.is_file():
            if "." in rel.rsplit("/", 1)[-1]:
                return self.send(404, b"Not found", {"Content-Type": "text/plain"})
            target = WEB_DIR / "index.html"  # single-page app route
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        cache = "public, max-age=31536000, immutable" if rel.startswith("assets/") else "no-cache"
        self.send(200, target.read_bytes(), {"Content-Type": ctype, "Cache-Control": cache})


# ---------------------------------------------------------------- main
def main() -> None:
    sys.path.insert(0, "/app")
    bootstrap()
    if not (ACCESS_CERTS_URL and ACCESS_AUD):
        log.warning("ACCESS_TEAM_DOMAIN / ACCESS_AUD not set: the API refuses signed-in routes until they are")

    ctx = mp.get_context("spawn")
    env = {k: v for k, v in os.environ.items() if k.endswith("_QUEUE_URL")}
    specs = []
    for queue, (handler, batch, procs) in CONSUMERS.items():
        for i in range(procs):
            specs.append((f"{queue}-{i}", worker, (queue, handler, batch, i, env)))
    specs.append(("dlq", dlq_worker, (env,)))
    specs.append(("sweeper", sweeper_loop, (env,)))

    procs: dict[str, mp.Process] = {}

    def start(name, target, args):
        p = ctx.Process(target=target, args=args, name=name, daemon=True)
        p.start()
        procs[name] = p

    for spec in specs:
        start(*spec)
    log.info("started %d worker processes (tesseract x%d)", len(procs), TESSERACT_WORKERS)

    def supervise():
        while True:
            time.sleep(10)
            for name, target, args in specs:
                if not procs[name].is_alive():
                    log.error("worker %s exited (%s); restarting", name, procs[name].exitcode)
                    start(name, target, args)

    threading.Thread(target=supervise, daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    # shutdown() blocks until serve_forever() returns, so it must run on another thread.
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
    log.info("listening on :%d", PORT)
    server.serve_forever()
    for p in procs.values():
        p.terminate()


if __name__ == "__main__":
    main()
