"""Tiny router for API Gateway HTTP API (payload v2) events."""
from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable
from typing import Any


class HttpError(Exception):
    def __init__(self, status: int, message: str, code: str = ""):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code or {
            400: "bad_request", 401: "unauthorized", 402: "payment_required", 403: "forbidden",
            404: "not_found", 409: "conflict",
        }.get(status, "error")


def response(status: int, body: Any = None, headers: dict | None = None) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", "cache-control": "no-store", **(headers or {})},
        "body": json.dumps(body if body is not None else {}, default=str),
    }


class Request:
    def __init__(self, event: dict, params: dict):
        self.event = event
        self.params = params
        self.method = event["requestContext"]["http"]["method"]
        self.path = event.get("rawPath", "/")
        self.query = event.get("queryStringParameters") or {}
        self.headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
        raw = event.get("body") or ""
        if event.get("isBase64Encoded") and raw:
            raw = base64.b64decode(raw).decode()
        self.raw_body = raw
        claims = (event["requestContext"].get("authorizer") or {}).get("jwt", {}).get("claims", {})
        self.sub: str | None = claims.get("sub")
        self.email: str = (claims.get("email") or "").lower()
        self.user: dict | None = None  # filled by the auth middleware

    def json(self) -> dict:
        if not self.raw_body:
            return {}
        try:
            data = json.loads(self.raw_body)
        except json.JSONDecodeError as exc:
            raise HttpError(400, "Body must be JSON") from exc
        if not isinstance(data, dict):
            raise HttpError(400, "Body must be a JSON object")
        return data


Handler = Callable[[Request], Any]


class Router:
    def __init__(self, prefix: str = ""):
        self.prefix = prefix.rstrip("/")
        self.routes: list[tuple[str, re.Pattern, Handler, dict]] = []

    def route(self, method: str, path: str, **opts):
        pattern = re.compile("^" + self.prefix + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", path) + "/?$")

        def deco(fn: Handler) -> Handler:
            self.routes.append((method.upper(), pattern, fn, opts))
            return fn

        return deco

    def match(self, method: str, path: str):
        path_found = False
        for m, pat, fn, opts in self.routes:
            mt = pat.match(path)
            if mt:
                path_found = True
                if m == method:
                    return fn, mt.groupdict(), opts
        raise HttpError(405 if path_found else 404, "Method not allowed" if path_found else "Not found")
