"""Lazily created boto3 clients (cached per Lambda container)."""
from __future__ import annotations

import functools

import boto3
from botocore.config import Config

_cfg = Config(retries={"max_attempts": 8, "mode": "adaptive"})


@functools.cache
def client(name: str):
    return boto3.client(name, config=_cfg)


@functools.cache
def resource(name: str):
    return boto3.resource(name, config=_cfg)


def reset() -> None:
    """Used by tests (moto) to drop cached clients."""
    client.cache_clear()
    resource.cache_clear()
