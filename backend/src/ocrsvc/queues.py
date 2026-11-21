"""SQS helpers."""
from __future__ import annotations

import json
from collections.abc import Iterable

from . import aws


def send(queue_url: str, body: dict, delay: int = 0) -> None:
    kwargs = {"QueueUrl": queue_url, "MessageBody": json.dumps(body)}
    if delay:
        kwargs["DelaySeconds"] = min(int(delay), 900)
    aws.client("sqs").send_message(**kwargs)


def send_many(queue_url: str, bodies: Iterable[dict]) -> int:
    """Send messages in batches of 10, retrying individual failures."""
    sqs = aws.client("sqs")
    batch: list[dict] = []
    count = 0

    def flush() -> None:
        nonlocal batch
        pending = batch
        for _ in range(5):
            if not pending:
                break
            resp = sqs.send_message_batch(QueueUrl=queue_url, Entries=pending)
            failed_ids = {f["Id"] for f in resp.get("Failed", [])}
            pending = [e for e in pending if e["Id"] in failed_ids]
        if pending:
            raise RuntimeError(f"Could not send {len(pending)} SQS messages")
        batch = []

    for body in bodies:
        batch.append({"Id": str(len(batch)), "MessageBody": json.dumps(body)})
        count += 1
        if len(batch) == 10:
            flush()
    if batch:
        flush()
    return count


def records(event: dict) -> Iterable[tuple[str, dict]]:
    """Yield (messageId, body) for each SQS record in a Lambda event."""
    for rec in event.get("Records", []):
        yield rec["messageId"], json.loads(rec["body"])


def run_batch(event: dict, handle) -> dict:
    """Process each record; report failures individually (ReportBatchItemFailures)."""
    import logging
    import traceback

    log = logging.getLogger("ocr")
    failures = []
    for message_id, body in records(event):
        try:
            handle(body)
        except Exception:  # noqa: BLE001 - we want SQS to retry the message
            log.error("message %s failed: %s\n%s", message_id, body, traceback.format_exc())
            failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": failures}
