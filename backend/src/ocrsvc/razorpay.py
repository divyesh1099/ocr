"""Minimal Razorpay client (orders + signature checks) with no extra dependencies."""
from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request

from . import aws, config, db, users

API = "https://api.razorpay.com/v1"


@functools.lru_cache(maxsize=1)
def _secret_cached(bucket: int) -> dict:  # bucket = time window, so rotated keys are picked up
    arn = config.RAZORPAY_SECRET_ARN()
    if not arn:
        return {}
    raw = aws.client("secretsmanager").get_secret_value(SecretId=arn)["SecretString"]
    return json.loads(raw)


def secret() -> dict:
    return _secret_cached(int(time.time() // 300))


def configured() -> bool:
    s = secret()
    return bool(s.get("keyId")) and bool(s.get("keySecret")) and not s["keyId"].startswith("CHANGE_ME")


def key_id() -> str:
    return secret().get("keyId", "")


def _call(method: str, path: str, body: dict | None = None) -> dict:
    s = secret()
    token = base64.b64encode(f"{s['keyId']}:{s['keySecret']}".encode()).decode()
    req = urllib.request.Request(
        API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Basic {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - fixed https endpoint
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Razorpay error {exc.code}: {exc.read()[:300]!r}") from exc


def create_order(sub: str, pack: dict, currency: str) -> dict:
    receipt = f"ocr_{db.new_id(8)}"
    order = _call("POST", "/orders", {
        "amount": int(pack["amount"]), "currency": currency, "receipt": receipt,
        "notes": {"sub": sub, "packId": pack["id"], "credits": str(pack["credits"])},
    })
    db.put({
        "PK": f"ORDER#{order['id']}", "SK": "ORDER",
        "orderId": order["id"], "sub": sub, "packId": pack["id"], "credits": int(pack["credits"]),
        "amount": int(pack["amount"]), "currency": currency, "status": "created", "createdAt": db.now_iso(),
        "GSI1PK": f"ORDERS#{sub}", "GSI1SK": db.now_iso(),
    })
    return order


def verify_payment_signature(order_id: str, payment_id: str, signature: str) -> bool:
    expected = hmac.new(secret()["keySecret"].encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def verify_webhook_signature(raw_body: str, signature: str) -> bool:
    wh = secret().get("webhookSecret", "")
    if not wh:
        return False
    expected = hmac.new(wh.encode(), raw_body.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def fulfil(order_id: str, payment_id: str) -> dict | None:
    """Credit the user for a paid order exactly once. Returns the order item (or None if unknown)."""
    key = {"PK": f"ORDER#{order_id}", "SK": "ORDER"}
    won = db.update(key, "SET #s = :paid, paymentId = :p, paidAt = :t",
                    {":paid": "paid", ":p": payment_id, ":t": db.now_iso()},
                    names={"#s": "status"}, condition="attribute_exists(PK) AND #s <> :paid")
    order = db.get(key, consistent=True)
    if won and order:
        users.grant_credits(order["sub"], int(order["credits"]), f"purchase {order['packId']}", order_id)
    return order
