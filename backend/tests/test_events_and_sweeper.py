"""Lifecycle events (EventBridge) and the scheduled sweeper safety net."""
import datetime as dt

from conftest import drain

from ocrsvc import aws as aws_mod
from ocrsvc import db, events, jobs
from ocrsvc.handlers import sweeper


class FakeEvents:
    def __init__(self):
        self.sent = []

    def put_events(self, Entries):
        self.sent.extend(Entries)
        return {"FailedEntryCount": 0, "Entries": [{"EventId": "x"}]}


def capture_events(monkeypatch):
    fake = FakeEvents()
    real_client = aws_mod.client
    monkeypatch.setattr(aws_mod, "client", lambda name: fake if name == "events" else real_client(name))
    monkeypatch.setenv("EVENT_BUS_NAME", "ocr-test-bus")
    return fake


def hours_ago(h: float) -> str:
    return (dt.datetime.now(dt.UTC) - dt.timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def test_emit_is_noop_without_bus(aws):
    events.emit("Job Submitted", {"jobId": "x"})  # must not raise or call AWS


def test_emit_never_raises(aws, monkeypatch):
    class Broken:
        def put_events(self, **kw):
            raise RuntimeError("eventbridge down")
    monkeypatch.setattr(aws_mod, "client", lambda name: Broken())
    monkeypatch.setenv("EVENT_BUS_NAME", "bus")
    events.emit("Job Submitted", {"jobId": "x"})


def test_submit_marks_active_and_emits(aws, monkeypatch):
    fake = capture_events(monkeypatch)
    job = jobs.create_job("u1", "j", "tesseract", ["txt"])
    assert jobs.submit(job["jobId"])
    stored = jobs.get_job(job["jobId"], consistent=True)
    assert stored["GSI2PK"] == "ACTIVE"
    assert [e["DetailType"] for e in fake.sent] == ["Job Submitted"]
    assert fake.sent[0]["Source"] == "ocr.pipeline" and fake.sent[0]["EventBusName"] == "ocr-test-bus"
    assert [j["jobId"] for j in sweeper.active_jobs()] == [job["jobId"]]


def test_sweeper_rechecks_finished_counter(aws):
    job = jobs.create_job("u1", "j", "tesseract", ["txt"])
    jobs.submit(job["jobId"])
    drain(aws, "ingest")
    db.update(db.job_key(job["jobId"]), "SET pending = :z", {":z": 0})  # completion check was lost
    stats = sweeper.handler({}, None)
    assert stats["rechecked"] == 1
    assert jobs.get_job(job["jobId"], consistent=True)["status"] == "finalizing"
    assert drain(aws, "finalize") == [{"jobId": job["jobId"]}]


def test_sweeper_resends_stuck_finalize_once(aws):
    job = jobs.create_job("u1", "j", "tesseract", ["txt"])
    jobs.submit(job["jobId"])
    db.update(db.job_key(job["jobId"]), "SET #s = :f, finalizingAt = :t", {":f": "finalizing", ":t": hours_ago(3)},
              names={"#s": "status"})
    assert sweeper.handler({}, None)["refinalized"] == 1
    assert sweeper.handler({}, None)["refinalized"] == 0  # finalizingAt was bumped
    assert drain(aws, "finalize") == [{"jobId": job["jobId"]}]


def test_sweeper_alerts_stalled_job_once(aws, monkeypatch):
    fake = capture_events(monkeypatch)
    job = jobs.create_job("u1", "j", "tesseract", ["txt"])
    jobs.submit(job["jobId"])
    db.update(db.job_key(job["jobId"]), "SET submittedAt = :t", {":t": hours_ago(10)})
    assert sweeper.handler({}, None)["stalled"] == 1
    assert sweeper.handler({}, None)["stalled"] == 0
    stalled = [e for e in fake.sent if e["DetailType"] == "Job Stalled"]
    assert len(stalled) == 1


def test_sweeper_drops_terminal_jobs_from_index(aws):
    job = jobs.create_job("u1", "j", "tesseract", ["txt"])
    jobs.submit(job["jobId"])
    db.update(db.job_key(job["jobId"]), "SET #s = :d", {":d": "done"}, names={"#s": "status"})
    sweeper.handler({}, None)
    assert list(sweeper.active_jobs()) == []
