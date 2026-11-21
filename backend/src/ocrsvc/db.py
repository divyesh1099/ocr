"""DynamoDB single-table access.

Item layout (PK / SK):
  USER#<sub>        PROFILE                 user profile (status, role, credits, ...)
  USER#<sub>        LEDGER#<iso>#<rand>     credit ledger entry
  INVITE#<email>    INVITE                  invitation (invite-only mode)
  SETTINGS          GLOBAL                  site settings (access mode, prices, packs)
  JOB#<jobId>       META                    job (counters, status)            GSI1: OWNER#<sub> / createdAt
  JOB#<jobId>       DOC#<docId>             one source document inside a job
  JOB#<jobId>       TASK#<id>               idempotency markers for queue tasks
  TEXTRACT#<id>     MAP                     Textract async JobId -> (job, doc, unit)
  ORDER#<orderId>   ORDER                   Razorpay order
"""
from __future__ import annotations

import datetime as _dt
import secrets
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

from . import aws, config

_ser = TypeSerializer()
_deser = TypeDeserializer()


def now_iso() -> str:
    return _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def new_id(nbytes: int = 9) -> str:
    return secrets.token_urlsafe(nbytes).replace("-", "a").replace("_", "b")


def table():
    return aws.resource("dynamodb").Table(config.TABLE_NAME())


def ddb():
    return aws.client("dynamodb")


# ---------------------------------------------------------------- keys
def user_key(sub: str) -> dict:
    return {"PK": f"USER#{sub}", "SK": "PROFILE"}


def job_key(job_id: str) -> dict:
    return {"PK": f"JOB#{job_id}", "SK": "META"}


def doc_key(job_id: str, doc_id: str) -> dict:
    return {"PK": f"JOB#{job_id}", "SK": f"DOC#{doc_id}"}


def invite_key(email: str) -> dict:
    return {"PK": f"INVITE#{email.strip().lower()}", "SK": "INVITE"}


SETTINGS_KEY = {"PK": "SETTINGS", "SK": "GLOBAL"}


# ---------------------------------------------------------------- helpers
def plain(value: Any) -> Any:
    """Convert DynamoDB Decimals into ints/floats so values are JSON friendly."""
    if isinstance(value, list):
        return [plain(v) for v in value]
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, set):
        return [plain(v) for v in value]
    return value


def to_ddb(value: Any) -> Any:
    """Convert floats to Decimal recursively (boto3 rejects floats)."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, list):
        return [to_ddb(v) for v in value]
    if isinstance(value, dict):
        return {k: to_ddb(v) for k, v in value.items()}
    return value


def get(key: dict, consistent: bool = False) -> dict | None:
    resp = table().get_item(Key=key, ConsistentRead=consistent)
    item = resp.get("Item")
    return plain(item) if item else None


def put(item: dict, condition: str | None = None, names: dict | None = None) -> bool:
    kwargs: dict[str, Any] = {"Item": to_ddb(item)}
    if condition:
        kwargs["ConditionExpression"] = condition
    if names:
        kwargs["ExpressionAttributeNames"] = names
    try:
        table().put_item(**kwargs)
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def update(key: dict, expr: str, values: dict | None = None, names: dict | None = None,
           condition: str | None = None, return_values: str = "ALL_NEW") -> dict | None:
    """UpdateItem; returns the new item, or None if the condition failed."""
    kwargs: dict[str, Any] = {"Key": key, "UpdateExpression": expr, "ReturnValues": return_values}
    if values:
        kwargs["ExpressionAttributeValues"] = to_ddb(values)
    if names:
        kwargs["ExpressionAttributeNames"] = names
    if condition:
        kwargs["ConditionExpression"] = condition
    try:
        resp = table().update_item(**kwargs)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return None
        raise
    return plain(resp.get("Attributes", {}))


def query(pk: str, sk_prefix: str | None = None, index: str | None = None, limit: int | None = None,
          cursor: dict | None = None, forward: bool = True) -> tuple[list[dict], dict | None]:
    from boto3.dynamodb.conditions import Key

    pk_name, sk_name = ("GSI1PK", "GSI1SK") if index == "GSI1" else ("PK", "SK")
    cond = Key(pk_name).eq(pk)
    if sk_prefix:
        cond = cond & Key(sk_name).begins_with(sk_prefix)
    kwargs: dict[str, Any] = {"KeyConditionExpression": cond, "ScanIndexForward": forward}
    if index:
        kwargs["IndexName"] = index
    if limit:
        kwargs["Limit"] = limit
    if cursor:
        kwargs["ExclusiveStartKey"] = cursor
    resp = table().query(**kwargs)
    return [plain(i) for i in resp.get("Items", [])], resp.get("LastEvaluatedKey")


def query_all(pk: str, sk_prefix: str | None = None) -> Iterable[dict]:
    cursor = None
    while True:
        items, cursor = query(pk, sk_prefix, cursor=cursor)
        yield from items
        if not cursor:
            return


# ---------------------------------------------------------------- transactions
def _typed(d: dict) -> dict:
    return {k: _ser.serialize(to_ddb(v)) for k, v in d.items()}


def tx_put(item: dict, condition: str | None = None, names: dict | None = None) -> dict:
    op: dict[str, Any] = {"TableName": config.TABLE_NAME(), "Item": _typed(item)}
    if condition:
        op["ConditionExpression"] = condition
    if names:
        op["ExpressionAttributeNames"] = names
    return {"Put": op}


def tx_update(key: dict, expr: str, values: dict | None = None, names: dict | None = None,
              condition: str | None = None) -> dict:
    op: dict[str, Any] = {"TableName": config.TABLE_NAME(), "Key": _typed(key), "UpdateExpression": expr}
    if values:
        op["ExpressionAttributeValues"] = _typed(values)
    if names:
        op["ExpressionAttributeNames"] = names
    if condition:
        op["ConditionExpression"] = condition
    return {"Update": op}


def tx_condition(key: dict, condition: str, values: dict | None = None, names: dict | None = None) -> dict:
    op: dict[str, Any] = {"TableName": config.TABLE_NAME(), "Key": _typed(key), "ConditionExpression": condition}
    if values:
        op["ExpressionAttributeValues"] = _typed(values)
    if names:
        op["ExpressionAttributeNames"] = names
    return {"ConditionCheck": op}


class TxConflict(Exception):
    """Raised when a transaction's condition fails. `reasons` lists per-item cancellation codes."""

    def __init__(self, reasons: list[str]):
        super().__init__(f"transaction cancelled: {reasons}")
        self.reasons = reasons


def transact(ops: list[dict]) -> None:
    try:
        ddb().transact_write_items(TransactItems=ops)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "TransactionCanceledException":
            reasons = [r.get("Code", "None") for r in exc.response.get("CancellationReasons", [])]
            if not reasons:  # moto / older SDKs: parse from the message
                msg = exc.response["Error"].get("Message", "")
                if "[" in msg:
                    reasons = [r.strip() for r in msg[msg.index("[") + 1: msg.rindex("]")].split(",")]
            raise TxConflict(reasons) from exc
        raise
