"""End-to-end smoke test for the self-hosted site, with a stand-in Cloudflare Access issuer.

  python selfhost/smoke_test.py keygen DIR                 # writes DIR/key.pem + DIR/certs.json (JWKS)
  python selfhost/smoke_test.py run BASE_URL DIR ISS AUD EMAIL

`run` signs in as EMAIL (a JWT signed with DIR/key.pem, sent as Cf-Access-Jwt-Assertion), creates a
Tesseract job, uploads a rendered-text PDF (single PUT) and a 70 MB file (multipart), submits,
waits for the job to finish and checks the OCR text inside the result zip.
"""
from __future__ import annotations

import io
import json
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path


def keygen(out: str) -> None:
    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    d = Path(out)
    d.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (d / "key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                  serialization.NoEncryption()))
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="smoke", alg="RS256", use="sig")
    (d / "certs.json").write_text(json.dumps({"keys": [jwk]}))
    print(f"wrote {d}/key.pem and {d}/certs.json")


def http(method: str, url: str, token: str | None = None, body=None, raw: bytes | None = None,
         headers: dict | None = None):
    h = dict(headers or {})
    data = raw
    if body is not None:
        data = json.dumps(body).encode()
        h["content-type"] = "application/json"
    if token:
        h["Cf-Access-Jwt-Assertion"] = token
    req = urllib.request.Request(url, method=method, data=data, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def text_pdf(lines: list[str]) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (1700, 2200), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 64)
    except OSError:
        font = ImageFont.load_default(size=64)
    for i, line in enumerate(lines):
        draw.text((120, 200 + i * 120), line, fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, "PDF", resolution=200)
    return buf.getvalue()


def run(base: str, keydir: str, iss: str, aud: str, email: str) -> None:
    import jwt

    now = int(time.time())
    token = jwt.encode({"iss": iss, "aud": [aud], "sub": "smoke-user-1", "email": email, "iat": now,
                        "exp": now + 3600, "type": "app"},
                       (Path(keydir) / "key.pem").read_text(), algorithm="RS256", headers={"kid": "smoke"})
    api = base.rstrip("/") + "/api"

    def call(method, path, body=None):
        status, data, _ = http(method, api + path, token, body)
        payload = json.loads(data or b"{}")
        if status >= 400:
            raise SystemExit(f"{method} {path} -> {status} {payload}")
        return payload

    # auth is enforced
    assert http("GET", api + "/me")[0] == 401, "API must reject requests without an Access token"
    bad = jwt.encode({"iss": iss, "aud": [aud], "sub": "x", "email": email, "iat": now, "exp": now + 60},
                     "not-the-key", algorithm="HS256")
    assert http("GET", api + "/me", bad)[0] == 401, "API must reject forged tokens"
    print("ok  unauthenticated and forged tokens rejected")

    me = call("GET", "/me")["user"]
    assert me["role"] == "superadmin" and me["status"] == "active", me
    print(f"ok  signed in as {me['email']} ({me['role']})")

    job = call("POST", "/jobs", {"name": "smoke", "engine": "tesseract", "outputs": ["txt", "json", "pdf"]})["job"]
    jid = job["jobId"]
    pdf = text_pdf(["Self hosted OCR smoke test", "The quick brown fox", "jumps over the lazy dog 12345"])
    big = b"\0" * (70 * 1024 * 1024)
    ups = call("POST", f"/jobs/{jid}/uploads", {"files": [{"path": "docs/sample.pdf", "size": len(pdf)},
                                                            {"path": "big.pdf", "size": len(big)}]})["uploads"]
    single, multi = ups
    assert single["method"] == "single" and multi["method"] == "multipart", ups
    status, data, _ = http("PUT", single["url"], raw=pdf, headers={"content-type": single["contentType"]})
    assert status == 200, f"single PUT failed: {status} {data[:300]!r}"
    parts = []
    for i, url in enumerate(multi["partUrls"]):
        chunk = big[i * multi["partSize"]:(i + 1) * multi["partSize"]]
        status, data, hdrs = http("PUT", url, raw=chunk)
        assert status == 200, f"part {i + 1} failed: {status} {data[:300]!r}"
        parts.append({"PartNumber": i + 1, "ETag": hdrs.get("ETag") or hdrs.get("etag")})
    call("POST", f"/jobs/{jid}/uploads/complete", {"key": multi["key"], "uploadId": multi["uploadId"], "parts": parts})
    print(f"ok  uploaded via presigned URLs (single PUT + {len(parts)}-part multipart)")

    call("POST", f"/jobs/{jid}/submit")
    deadline = time.time() + 300
    while time.time() < deadline:
        job = call("GET", f"/jobs/{jid}")["job"]
        if job["status"] in ("done", "failed"):
            break
        time.sleep(2)
    assert job["status"] == "done", job
    print(f"ok  job done: {job['docsDone']} done, {job['docsFailed']} failed, {job['pagesDone']} pages")

    url = call("GET", f"/jobs/{jid}/download")["url"]
    status, data, _ = http("GET", url)
    assert status == 200, status
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        text = z.read("docs/sample.pdf.txt").decode()
        manifest = z.read("manifest.csv").decode()
    assert "quick brown fox" in text.lower(), text
    assert "docs/sample.pdf.pdf" in names and "big.pdf" in manifest, names
    print(f"ok  result zip: {sorted(names)}")
    print("ok  OCR text:", " | ".join(t.strip() for t in text.splitlines() if t.strip()))
    print("SMOKE TEST PASSED")


if __name__ == "__main__":
    if sys.argv[1] == "keygen":
        keygen(sys.argv[2])
    else:
        run(*sys.argv[2:7])
