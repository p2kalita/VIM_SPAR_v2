import threading
from datetime import timedelta

import pytest

import factories as f
from vim.gmail_tracking import reconciler as r
from vim.gmail_tracking.models import (
    GmailIngestedAttachment,
    GmailIngestReconcile,
)
from vim.timezone import get_ist_now


@pytest.fixture
def jobs(monkeypatch):
    """Fake in-memory job store keyed by job id."""
    store = {}
    from vim.extraction import jobs as real_jobs

    monkeypatch.setattr(real_jobs, "get_job", lambda job_id: store.get(job_id))
    return store


def _record(original, stored, *, status="success", invoice_id=None, error=None):
    return {
        "file_name": original,
        "stored_file_name": stored,
        "file_path": f"uploads/invoices/{stored}",
        "status": status,
        "invoice_id": invoice_id,
        "_extraction_error": error,
    }


def _rows(msg):
    return GmailIngestedAttachment.query.filter_by(IngestID=msg.IngestID).order_by(
        GmailIngestedAttachment.AttachmentID).all()


def _rec(msg):
    from vim_database.database import db

    db.session.expire_all()
    return db.session.get(GmailIngestReconcile, msg.IngestID)


# ---- 4.1 candidate selection -------------------------------------------------

def test_candidates_only_recent_ingested_with_job(ctx):
    good = f.message(job_id="j1")
    f.message(status="skipped", job_id=None)
    f.message(status="error", job_id=None)
    f.message(job_id=None)
    f.message(job_id="j-old", created=get_ist_now() - timedelta(days=30))
    assert [m.IngestID for m in r.candidates()] == [good.IngestID]


def test_settled_emails_not_candidates(ctx):
    from vim_database.database import db

    linked = f.message(job_id="j1")
    gave_up = f.message(job_id="j2")
    waiting = f.message(job_id="j3")
    db.session.add_all([
        GmailIngestReconcile(IngestID=linked.IngestID, State="linked"),
        GmailIngestReconcile(IngestID=gave_up.IngestID, State="unlinkable"),
        GmailIngestReconcile(IngestID=waiting.IngestID, State="waiting"),
    ])
    db.session.commit()
    assert [m.IngestID for m in r.candidates()] == [waiting.IngestID]


def test_skipped_and_old_never_get_rows(app, jobs):
    with app.app_context():
        skipped = f.message(status="skipped", job_id=None)
        old = f.message(job_id="j-old", created=get_ist_now() - timedelta(days=30))
        jobs["j-old"] = {"status": "complete", "results": [_record("invoice.pdf", "x_invoice.pdf")], "files": []}
        ids = (skipped.IngestID, old.IngestID)
    r.reconcile_once(app)
    with app.app_context():
        assert GmailIngestedAttachment.query.filter(
            GmailIngestedAttachment.IngestID.in_(ids)).count() == 0


# ---- 4.2 capture from the job ----------------------------------------------

def test_complete_job_links_rows(ctx, jobs):
    msg = f.message(names=("a.pdf", "po.pdf"), job_id="j1")
    jobs["j1"] = {
        "status": "complete",
        "files": [{"file_name": "a.pdf", "status": "success"},
                  {"file_name": "po.pdf", "status": "not_invoice"}],
        "results": [_record("a.pdf", "u1_a.pdf", invoice_id=7),
                    _record("po.pdf", "u2_po.pdf", status="not_invoice")],
    }
    assert r.reconcile_message(msg) == "linked"
    rows = _rows(msg)
    assert [(x.OriginalName, x.StoredFileName, x.CapturedStatus, x.CapturedInvoiceID, x.LinkMethod)
            for x in rows] == [
        ("a.pdf", "u1_a.pdf", "success", 7, "job"),
        ("po.pdf", "u2_po.pdf", "not_invoice", None, "job"),
    ]
    assert _rec(msg).State == "linked" and _rec(msg).Method == "job"


def test_running_job_stays_waiting(ctx, jobs):
    msg = f.message(job_id="j1")
    jobs["j1"] = {"status": "running", "files": [], "results": []}
    assert r.reconcile_message(msg) == "waiting"
    assert _rows(msg) == []
    assert _rec(msg).State == "waiting"


def test_errored_file_without_record(ctx, jobs):
    msg = f.message(names=("a.pdf", "b.pdf"), job_id="j1")
    jobs["j1"] = {
        "status": "complete",
        "files": [{"file_name": "a.pdf", "status": "success"},
                  {"file_name": "b.pdf", "status": "error", "detail": "worker crashed"}],
        "results": [_record("a.pdf", "u1_a.pdf", invoice_id=1)],
    }
    r.reconcile_message(msg)
    rows = _rows(msg)
    assert len(rows) == 2
    err = rows[1]
    assert (err.OriginalName, err.StoredFileName, err.CapturedStatus, err.CapturedDetail) == (
        "b.pdf", None, "error", "worker crashed")


def test_extraction_failed_record_keeps_detail(ctx, jobs):
    msg = f.message(job_id="j1")
    jobs["j1"] = {
        "status": "complete",
        "files": [{"file_name": "invoice.pdf", "status": "extraction_failed"}],
        "results": [_record("invoice.pdf", "u_invoice.pdf", status="extraction_failed", error="parse failed")],
    }
    r.reconcile_message(msg)
    (row,) = _rows(msg)
    assert row.CapturedStatus == "extraction_failed"
    assert row.CapturedDetail == "parse failed"


# ---- 4.3 fallback ------------------------------------------------------------

def test_fallback_unique_match_links(ctx, jobs):
    msg = f.message(names=("po.pdf",), job_id="gone")
    rej = f.rejection(stored_name="u9_po.pdf", file_name="po.pdf", created=msg.CreatedDate)
    assert r.reconcile_message(msg) == "linked"
    (row,) = _rows(msg)
    assert (row.StoredFileName, row.LinkMethod) == (rej.StoredFileName, "fallback")
    assert _rec(msg).Method == "fallback"


def test_fallback_links_invoice_with_id(ctx, jobs):
    msg = f.message(names=("a.pdf",), job_id="gone")
    inv = f.invoice(storage_path="G:\\abs\\uploads\\u5_a.pdf", file_name="a.pdf",
                    uploaded=msg.CreatedDate + timedelta(minutes=1))
    r.reconcile_message(msg)
    (row,) = _rows(msg)
    assert row.StoredFileName == "u5_a.pdf"
    assert row.CapturedInvoiceID == inv.InvoiceID


def test_fallback_proceeded_document_counts_once(ctx, jobs):
    msg = f.message(names=("a.pdf",), job_id="gone")
    f.rejection(stored_name="u6_a.pdf", file_name="a.pdf", decision="proceeded", created=msg.CreatedDate)
    f.invoice(storage_path="uploads/invoices/u6_a.pdf", file_name="a.pdf", uploaded=msg.CreatedDate)
    assert r.reconcile_message(msg) == "linked"
    assert [x.StoredFileName for x in _rows(msg)] == ["u6_a.pdf"]


def test_fallback_ambiguous_links_nothing(ctx, jobs):
    msg = f.message(names=("invoice.pdf",), job_id="gone")
    f.rejection(stored_name="u1_invoice.pdf", created=msg.CreatedDate)
    f.rejection(stored_name="u2_invoice.pdf", created=msg.CreatedDate)
    assert r.reconcile_message(msg) == "waiting"
    assert _rows(msg) == []


def test_fallback_excludes_claimed_file(ctx, jobs):
    other = f.message(names=("invoice.pdf",), job_id="j-other")
    f.attachment(other, original="invoice.pdf", stored_name="u1_invoice.pdf")
    msg = f.message(names=("invoice.pdf",), job_id="gone")
    f.rejection(stored_name="u1_invoice.pdf", created=msg.CreatedDate)
    f.rejection(stored_name="u2_invoice.pdf", created=msg.CreatedDate)
    assert r.reconcile_message(msg) == "linked"
    assert [x.StoredFileName for x in _rows(msg)] == ["u2_invoice.pdf"]


def test_fallback_ignores_outside_window(ctx, jobs):
    msg = f.message(names=("po.pdf",), job_id="gone")
    f.rejection(stored_name="u1_po.pdf", file_name="po.pdf", created=msg.CreatedDate + timedelta(hours=5))
    f.rejection(stored_name="u2_po.pdf", file_name="po.pdf", created=msg.CreatedDate - timedelta(hours=1))
    assert r.reconcile_message(msg) == "waiting"
    assert _rows(msg) == []


def test_fallback_gives_up_after_three_attempts(ctx, jobs):
    msg = f.message(names=("po.pdf",), job_id="gone")
    assert r.reconcile_message(msg) == "waiting"
    assert r.reconcile_message(msg) == "waiting"
    assert r.reconcile_message(msg) == "unlinkable"
    assert _rec(msg).Attempts == 3


def test_same_name_twice_in_one_email_not_linked(ctx, jobs):
    msg = f.message(names=("invoice.pdf", "invoice.pdf"), job_id="gone")
    f.rejection(stored_name="u1_invoice.pdf", created=msg.CreatedDate)
    assert r.reconcile_message(msg) == "waiting"


# ---- 4.4 idempotency ---------------------------------------------------------

def _complete_job(jobs, job_id):
    jobs[job_id] = {
        "status": "complete",
        "files": [{"file_name": "a.pdf", "status": "success"},
                  {"file_name": "b.pdf", "status": "error", "detail": "boom"}],
        "results": [_record("a.pdf", "u1_a.pdf", invoice_id=1)],
    }


def test_reconcile_twice_no_duplicates(ctx, jobs):
    msg = f.message(names=("a.pdf", "b.pdf"), job_id="j1")
    _complete_job(jobs, "j1")
    r.reconcile_message(msg)
    r.reconcile_message(msg)
    assert len(_rows(msg)) == 2


def test_two_cycles_back_to_back(app, jobs):
    with app.app_context():
        msg = f.message(names=("a.pdf", "b.pdf"), job_id="j1")
        ingest_id = msg.IngestID
    _complete_job(jobs, "j1")
    first = r.reconcile_once(app)
    second = r.reconcile_once(app)
    assert first["linked"] == 1 and first["errors"] == 0
    assert second["checked"] == 0
    with app.app_context():
        assert GmailIngestedAttachment.query.filter_by(IngestID=ingest_id).count() == 2


def test_concurrent_cycles_no_duplicates(app, jobs):
    with app.app_context():
        msg = f.message(names=("a.pdf", "b.pdf"), job_id="j1")
        ingest_id = msg.IngestID
    _complete_job(jobs, "j1")
    threads = [threading.Thread(target=r.reconcile_once, args=(app,)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    r.reconcile_once(app)
    with app.app_context():
        assert GmailIngestedAttachment.query.filter_by(IngestID=ingest_id).count() == 2


def test_cycle_survives_bad_message(app, jobs, monkeypatch):
    with app.app_context():
        f.message(job_id="j-bad")
        good = f.message(job_id="j-good")
        good_id = good.IngestID
    jobs["j-good"] = {"status": "complete", "files": [], "results": [_record("invoice.pdf", "ug_invoice.pdf")]}

    real = r.reconcile_message

    def flaky(message, now=None):
        if message.JobID == "j-bad":
            raise RuntimeError("boom")
        return real(message, now=now)

    monkeypatch.setattr(r, "reconcile_message", flaky)
    summary = r.reconcile_once(app)
    assert summary["errors"] == 1 and summary["linked"] == 1
    with app.app_context():
        assert GmailIngestedAttachment.query.filter_by(IngestID=good_id).count() == 1


# ---- 4.5 lifecycle -----------------------------------------------------------

@pytest.fixture
def fresh_lifecycle(monkeypatch):
    monkeypatch.setattr(r, "_started", False)
    started = []
    monkeypatch.setattr(r, "_loop", lambda app: started.append(app))
    return started


def test_start_once(monkeypatch, fresh_lifecycle):
    monkeypatch.setenv("GMAIL_TRACKING", "1")
    assert r.start_reconciler("app") is True
    assert r.start_reconciler("app") is False


@pytest.mark.parametrize("value", ["0", "false", "off", "OFF"])
def test_off_switch(monkeypatch, fresh_lifecycle, value):
    monkeypatch.setenv("GMAIL_TRACKING", value)
    assert r.start_reconciler("app") is False
    assert r._started is False


class _Stop(BaseException):
    pass


def test_loop_sleeps_first_and_survives_errors(monkeypatch):
    calls = []
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > 3:
            raise _Stop

    def failing_cycle(app):
        calls.append(len(sleeps))
        raise RuntimeError("cycle blew up")

    monkeypatch.setattr(r.time, "sleep", fake_sleep)
    monkeypatch.setattr(r, "reconcile_once", failing_cycle)
    with pytest.raises(_Stop):
        r._loop("app")
    # Slept before the first cycle, and kept looping after each failure.
    assert calls == [1, 2, 3]


@pytest.mark.parametrize("raw,expected", [("", 30), ("5", 10), ("9999", 600), ("abc", 30), ("45", 45)])
def test_poll_seconds_clamped(monkeypatch, raw, expected):
    from vim.gmail_tracking import settings

    monkeypatch.setenv("GMAIL_TRACKING_SECONDS", raw)
    assert settings.poll_seconds() == expected
