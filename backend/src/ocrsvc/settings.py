"""Global site settings, editable by the superadmin."""
from __future__ import annotations

import copy
import time

from . import db

ACCESS_MODES = ("invite", "approval", "open")
ENGINES = ("textract", "tesseract")

DEFAULTS: dict = {
    # invite   = only emails the superadmin invited can sign up (they are active immediately)
    # approval = anyone can sign up, but stays "pending" until the superadmin approves
    # open     = anyone can sign up and use the service right away (pay with credits)
    "accessMode": "invite",
    # When false nobody is charged credits (handy for a private, invite-only deployment)
    "billingEnabled": False,
    "freeCreditsOnSignup": 50,
    "defaultEngine": "textract",
    "enabledEngines": ["textract", "tesseract"],
    # credits charged per page
    "prices": {"textract": 2, "tesseract": 1},
    "currency": "INR",
    "packs": [
        {"id": "starter", "name": "Starter", "credits": 1000, "amount": 19900},
        {"id": "pro", "name": "Pro", "credits": 10000, "amount": 149900},
        {"id": "scale", "name": "Scale", "credits": 100000, "amount": 999900},
    ],
    "maxFileBytes": 20 * 1024 * 1024 * 1024,
    "siteName": "Om",
}

_cache: dict = {"value": None, "at": 0.0}
_TTL = 30.0


def get(fresh: bool = False) -> dict:
    if not fresh and _cache["value"] is not None and time.time() - _cache["at"] < _TTL:
        return copy.deepcopy(_cache["value"])
    item = db.get(db.SETTINGS_KEY) or {}
    merged = copy.deepcopy(DEFAULTS)
    for k in DEFAULTS:
        if k in item:
            merged[k] = item[k]
    _cache.update(value=merged, at=time.time())
    return copy.deepcopy(merged)


class SettingsError(ValueError):
    pass


def validate(patch: dict) -> dict:
    clean: dict = {}
    for key, value in patch.items():
        if key not in DEFAULTS:
            raise SettingsError(f"Unknown setting: {key}")
        if key == "accessMode" and value not in ACCESS_MODES:
            raise SettingsError(f"accessMode must be one of {ACCESS_MODES}")
        if key in ("billingEnabled",) and not isinstance(value, bool):
            raise SettingsError(f"{key} must be true/false")
        if key in ("freeCreditsOnSignup", "maxFileBytes"):
            if not isinstance(value, int) or value < 0:
                raise SettingsError(f"{key} must be a non-negative integer")
        if key == "defaultEngine" and value not in ENGINES:
            raise SettingsError(f"defaultEngine must be one of {ENGINES}")
        if key == "enabledEngines":
            if not isinstance(value, list) or not value or any(v not in ENGINES for v in value):
                raise SettingsError(f"enabledEngines must be a non-empty subset of {ENGINES}")
        if key == "prices":
            if not isinstance(value, dict) or any(
                e not in ENGINES or not isinstance(p, int) or p < 0 for e, p in value.items()
            ):
                raise SettingsError("prices must map engine -> non-negative integer credits per page")
        if key == "packs":
            if not isinstance(value, list):
                raise SettingsError("packs must be a list")
            ids = set()
            for p in value:
                if not (isinstance(p, dict) and isinstance(p.get("id"), str) and p["id"]
                        and isinstance(p.get("credits"), int) and p["credits"] > 0
                        and isinstance(p.get("amount"), int) and p["amount"] >= 100):
                    raise SettingsError("each pack needs id, name, credits (>0) and amount (in paise, >=100)")
                if p["id"] in ids:
                    raise SettingsError(f"duplicate pack id {p['id']}")
                ids.add(p["id"])
        clean[key] = value
    return clean


def save(patch: dict) -> dict:
    clean = validate(patch)
    current = get(fresh=True)
    current.update(clean)
    db.put({**db.SETTINGS_KEY, **current, "updatedAt": db.now_iso()})
    _cache.update(value=None, at=0.0)
    return get(fresh=True)


def price_per_page(engine: str) -> int:
    return int(get()["prices"].get(engine, 0))
