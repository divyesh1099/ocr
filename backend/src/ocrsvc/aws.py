"""Lazily created boto3 clients (cached per Lambda container).

Endpoints come from the standard AWS_ENDPOINT_URL_<SERVICE> variables, so the same code runs
against AWS or against S3/SQS/DynamoDB-compatible services (selfhost/). PUBLIC_S3_ENDPOINT,
when set, is the address browsers use for presigned URLs (the internal one may be unreachable).
"""
from __future__ import annotations

import functools
import os

import boto3
from botocore.config import Config

_cfg = Config(
    retries={"max_attempts": 8, "mode": "adaptive"},
    s3={"addressing_style": os.environ.get("S3_ADDRESSING_STYLE", "auto")},
)


@functools.cache
def client(name: str):
    return boto3.client(name, config=_cfg)


@functools.cache
def resource(name: str):
    return boto3.resource(name, config=_cfg)


@functools.cache
def presign_client():
    """S3 client used only to sign URLs that browsers will call."""
    public = os.environ.get("PUBLIC_S3_ENDPOINT")
    if not public:
        return client("s3")
    return boto3.client("s3", endpoint_url=public, config=_cfg)


def reset() -> None:
    """Used by tests (moto) to drop cached clients."""
    client.cache_clear()
    resource.cache_clear()
    presign_client.cache_clear()
