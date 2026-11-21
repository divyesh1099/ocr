"""Cognito User Pool triggers: enforce the access mode at sign-up, create profiles on confirmation."""
from __future__ import annotations

import logging

from .. import users

log = logging.getLogger("ocr")


def pre_sign_up(event, context):
    email = (event.get("request", {}).get("userAttributes", {}).get("email") or "").lower()
    allowed, reason = users.signup_allowed(email)
    if not allowed:
        # Cognito shows this message to the user ("PreSignUp failed with error <reason>")
        raise Exception(reason)
    return event


def post_confirmation(event, context):
    if event.get("triggerSource") != "PostConfirmation_ConfirmSignUp":
        return event
    attrs = event.get("request", {}).get("userAttributes", {})
    sub, email = attrs.get("sub"), attrs.get("email", "")
    if sub:
        profile = users.create_profile(sub, email)
        log.info("created profile %s (%s, %s)", sub, profile.get("role"), profile.get("status"))
    return event
