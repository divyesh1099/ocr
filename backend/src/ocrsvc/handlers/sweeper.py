"""Scheduled sweeper (EventBridge Scheduler, every 15 minutes).

The pipeline is designed so that every job finishes on its own: DLQs settle work that
failed repeatedly, and the pending counter reaches zero exactly once. The sweeper is a
safety net for the rare cases that slip through (a lost message, an AWS incident):

  * processing, pending <= 0      -> run the completion check again (moves it to finalizing)
  * finalizing for over 2 hours   -> send the finalize message again (finalize is idempotent)
  * processing for over STALL_HOURS -> publish one "Job Stalled" event (emails the admin);
                                     the superadmin can then use "force finish" in Admin

It only reads jobs that are active (sparse GSI2), so it costs nothing when the site is idle.
"""
from __future__ import annotations

import datetime as dt
import logging

from .. import config, db, events, jobs, queues

log = logging.getLogger("ocr")
log.setLevel(logging.INFO)

FINALIZE_RETRY_AFTER = dt.timedelta(hours=2)


def _age(iso: str | None, now: dt.datetime) -> dt.timedelta:
    if not iso:
        return dt.timedelta(0)
    return now - dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=dt.UTC)


def active_jobs():
    cursor = None
    while True:
        items, cursor = db.query(jobs.ACTIVE, index="GSI2", cursor=cursor)
        yield from items
        if not cursor:
            return


def handler(event, context):
    now = dt.datetime.now(dt.UTC)
    stall_after = dt.timedelta(hours=config.STALL_HOURS())
    stats = {"active": 0, "rechecked": 0, "refinalized": 0, "stalled": 0}
    for job in active_jobs():
        stats["active"] += 1
        try:
            sweep_one(job, now, stall_after, stats)
        except Exception:  # noqa: BLE001 - one bad job must not stop the sweep
            log.exception("sweeping job %s failed", job.get("jobId"))
    log.info("sweep done: %s", stats)
    return stats


def sweep_one(job: dict, now: dt.datetime, stall_after: dt.timedelta, stats: dict) -> None:
    job_id = job["jobId"]
    if job["status"] == "processing" and job.get("pending", 1) <= 0:
        jobs.check_finished(job_id)
        stats["rechecked"] += 1
        return

    if job["status"] == "finalizing" and _age(job.get("finalizingAt"), now) > FINALIZE_RETRY_AFTER:
        # Bump finalizingAt first, so overlapping sweeps only resend once.
        if db.update(db.job_key(job_id), "SET finalizingAt = :t", {":t": db.now_iso(), ":f": "finalizing",
                     ":old": job.get("finalizingAt")}, names={"#s": "status"},
                     condition="#s = :f AND finalizingAt = :old"):
            queues.send(config.FINALIZE_QUEUE_URL(), {"jobId": job_id})
            stats["refinalized"] += 1
        return

    if job["status"] == "processing" and _age(job.get("submittedAt"), now) > stall_after:
        if db.update(db.job_key(job_id), "SET stallAlertedAt = :t", {":t": db.now_iso()},
                     condition="attribute_not_exists(stallAlertedAt)"):
            events.emit("Job Stalled", {**events.job_summary(job), "pending": job.get("pending"),
                                        "hoursRunning": round(_age(job.get("submittedAt"), now).total_seconds()
                                                              / 3600, 1)})
            stats["stalled"] += 1
        return

    if job["status"] not in ("processing", "finalizing"):
        # Terminal job still marked active (e.g. force-set by hand): drop it from the index.
        db.update(db.job_key(job_id), jobs.CLEAR_ACTIVE)
