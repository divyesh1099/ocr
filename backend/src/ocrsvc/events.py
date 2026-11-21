"""Job lifecycle events on the EventBridge bus.

Events are notifications for people and other systems (audit log, alerts, future
integrations). They never drive the pipeline itself: work is passed through SQS. So
publishing is best effort and a failure here never fails a task.

  source:       ocr.pipeline
  detail-type:  Job Submitted | Job Completed | Job Failed | Job Stalled | Document Failed
"""
from __future__ import annotations

import json
import logging

from . import aws, config

log = logging.getLogger("ocr")

SOURCE = "ocr.pipeline"


def emit(detail_type: str, detail: dict) -> None:
    bus = config.EVENT_BUS_NAME()
    if not bus:
        return
    try:
        resp = aws.client("events").put_events(Entries=[{
            "EventBusName": bus,
            "Source": SOURCE,
            "DetailType": detail_type,
            "Detail": json.dumps(detail, default=str),
        }])
        if resp.get("FailedEntryCount"):
            log.warning("event %s not published: %s", detail_type, resp.get("Entries"))
    except Exception:  # noqa: BLE001 - events are best effort
        log.exception("could not publish event %s", detail_type)


def job_summary(job: dict) -> dict:
    keys = ("jobId", "owner", "name", "engine", "status", "docsTotal", "docsDone", "docsFailed", "docsSkipped",
            "pagesTotal", "pagesDone", "creditsCharged", "createdAt", "submittedAt", "finishedAt", "lastError")
    return {k: job.get(k) for k in keys if job.get(k) is not None}
