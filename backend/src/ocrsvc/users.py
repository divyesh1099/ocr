"""User profiles, access control and credits."""
from __future__ import annotations

from . import config, db, settings

STATUSES = ("pending", "active", "banned")


def get_profile(sub: str) -> dict | None:
    return db.get(db.user_key(sub))


def is_superadmin_email(email: str) -> bool:
    admin = config.SUPERADMIN_EMAIL()
    return bool(admin) and email.strip().lower() == admin


def signup_allowed(email: str) -> tuple[bool, str]:
    """Decide whether a new account with this email may be created (Cognito pre-sign-up)."""
    email = email.strip().lower()
    if is_superadmin_email(email):
        return True, ""
    mode = settings.get(fresh=True)["accessMode"]
    if mode in ("open", "approval"):
        return True, ""
    if db.get(db.invite_key(email)):
        return True, ""
    return False, "Sign-up is invite only. Ask the administrator for an invitation."


def create_profile(sub: str, email: str) -> dict:
    """Create the profile right after the user confirms their email. Idempotent."""
    existing = get_profile(sub)
    if existing:
        return existing
    email = email.strip().lower()
    s = settings.get(fresh=True)
    role = "superadmin" if is_superadmin_email(email) else "user"
    invite = db.get(db.invite_key(email))
    if role == "superadmin" or invite or s["accessMode"] == "open":
        status = "active"
    else:
        status = "pending"
    credits = int(invite.get("credits", s["freeCreditsOnSignup"])) if invite else int(s["freeCreditsOnSignup"])
    now = db.now_iso()
    item = {
        **db.user_key(sub),
        "sub": sub,
        "email": email,
        "role": role,
        "status": status,
        "credits": credits,
        "unlimited": role == "superadmin",
        "createdAt": now,
        "GSI1PK": "USERS",
        "GSI1SK": now,
    }
    db.put(item, condition="attribute_not_exists(PK)")
    if credits:
        add_ledger(sub, credits, "signup bonus")
    if invite:
        db.update(db.invite_key(email), "SET usedAt = :t, usedBy = :s", {":t": now, ":s": sub})
    return get_profile(sub) or item


def ledger_item(sub: str, delta: int, reason: str, ref: str = "") -> dict:
    return {
        "PK": f"USER#{sub}",
        "SK": f"LEDGER#{db.now_iso()}#{db.new_id(4)}",
        "delta": delta,
        "reason": reason,
        "ref": ref,
        "at": db.now_iso(),
    }


def add_ledger(sub: str, delta: int, reason: str, ref: str = "") -> None:
    db.put(ledger_item(sub, delta, reason, ref))


def billing_applies(profile: dict) -> bool:
    return settings.get()["billingEnabled"] and not profile.get("unlimited")


def grant_credits(sub: str, delta: int, reason: str, ref: str = "") -> dict | None:
    """Add (or remove, if negative) credits. Never lets the balance go below zero."""
    if delta >= 0:
        new = db.update(db.user_key(sub), "ADD credits :d", {":d": delta}, condition="attribute_exists(PK)")
    else:
        new = db.update(db.user_key(sub), "ADD credits :d", {":d": delta, ":need": -delta},
                        condition="attribute_exists(PK) AND credits >= :need")
    if new is not None:
        add_ledger(sub, delta, reason, ref)
    return new
