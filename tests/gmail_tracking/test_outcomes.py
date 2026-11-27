from datetime import timedelta

import pytest
from sqlalchemy import event

import factories as f
from vim.gmail_tracking import outcomes as o
from vim.timezone import get_ist_now


def _state(row):
    (outcome,) = o.derive_attachments([row])
    return outcome


# ---- 3.1 attachment states --------------------------------------------------

def test_processed_when_invoice_document_exists(ctx):
    msg = f.message()
    row = f.attachment(msg, status="success")
    inv = f.invoice(storage_path=row.StoragePath)
    out = _state(row)
    assert out.state == o.PROCESSED
    assert out.invoice_id == inv.InvoiceID


@pytest.mark.parametrize("reason", ["not_invoice", "vendor_not_registered", "incomplete_header"])
def test_pending_when_held_for_review(ctx, reason):
    msg = f.message()
    row = f.attachment(msg, status=reason)
    rej = f.rejection(stored_name=row.StoredFileName, reason=reason, decision="pending")
    out = _state(row)
    assert out.state == o.PENDING
    assert out.rejection_id == rej.RejectionID


@pytest.mark.parametrize("reason", ["empty", "irrelevant"])
def test_not_invoice_when_auto_rejected(ctx, reason):
    msg = f.message()
    row = f.attachment(msg, status=reason)
    f.rejection(stored_name=row.StoredFileName, reason=reason, decision="auto_rejected")
    assert _state(row).state == o.NOT_INVOICE


def test_dismissed_when_stopped(ctx):
    msg = f.message()
    row = f.attachment(msg, status="not_invoice")
    f.rejection(stored_name=row.StoredFileName, decision="stopped")
    assert _state(row).state == o.DISMISSED


@pytest.mark.parametrize("status", ["extraction_failed", "db_error", "error"])
def test_error_when_pipeline_failed(ctx, status):
    msg = f.message()
    row = f.attachment(msg, status=status, detail="OCR timed out")
    out = _state(row)
    assert out.state == o.ERROR
    assert out.detail == "OCR timed out"


def test_error_row_without_stored_file(ctx):
    msg = f.message()
    row = f.attachment(msg, stored_name="", status="error", detail="worker crashed")
    assert _state(row).state == o.ERROR


def test_in_progress_without_outcome(ctx):
    msg = f.message()
    row = f.attachment(msg, status=None)
    assert _state(row).state == o.IN_PROGRESS


def test_proceeded_without_invoice_is_pending(ctx):
    msg = f.message()
    row = f.attachment(msg, status="vendor_not_registered")
    f.rejection(stored_name=row.StoredFileName, decision="proceeded")
    assert _state(row).state == o.PENDING


def test_captured_invoice_deleted_is_error(ctx):
    msg = f.message()
    row = f.attachment(msg, status="success", invoice_id=999999)
    out = _state(row)
    assert out.state == o.ERROR
    assert "999999" in out.detail


def test_captured_invoice_without_document_row_is_processed(ctx):
    msg = f.message()
    inv = f.invoice(storage_path=None)
    row = f.attachment(msg, status="success", invoice_id=inv.InvoiceID)
    assert _state(row).state == o.PROCESSED


# ---- 3.2 later decisions ----------------------------------------------------

def test_pending_flips_to_processed_after_proceed(ctx):
    msg = f.message()
    row = f.attachment(msg, status="not_invoice")
    rej = f.rejection(stored_name=row.StoredFileName, decision="pending")
    assert _state(row).state == o.PENDING

    from vim_database.database import db

    rej.Decision = "proceeded"
    db.session.commit()
    f.invoice(storage_path=row.StoragePath)
    assert _state(row).state == o.PROCESSED


def test_pending_flips_to_dismissed_after_stop(ctx):
    msg = f.message()
    row = f.attachment(msg, status="not_invoice")
    rej = f.rejection(stored_name=row.StoredFileName, decision="pending")

    from vim_database.database import db

    rej.Decision = "stopped"
    db.session.commit()
    assert _state(row).state == o.DISMISSED


@pytest.mark.parametrize("prefix", ["G:\\app\\uploads\\invoices\\", "/srv/app/uploads/invoices/"])
def test_absolute_path_still_matches_by_file_name(ctx, prefix):
    msg = f.message()
    row = f.attachment(msg, status="vendor_not_registered")
    f.rejection(stored_name=row.StoredFileName, decision="proceeded")
    # Force-processing stores the resolved absolute path, not the staged one.
    f.invoice(storage_path=prefix + row.StoredFileName)
    assert _state(row).state == o.PROCESSED


def test_other_file_with_same_suffix_does_not_match(ctx):
    msg = f.message()
    row = f.attachment(msg, stored_name="abc_invoice.pdf", status=None)
    f.invoice(storage_path="uploads/invoices/xabc_invoice.pdf")
    assert _state(row).state == o.IN_PROGRESS


# ---- 3.3 rollup and legacy --------------------------------------------------

def _email(msg):
    (email,) = o.derive_emails([msg])
    return email


def test_mixed_invoice_and_pending_rolls_up_pending(ctx):
    msg = f.message(names=("a.pdf", "po.pdf"))
    a = f.attachment(msg, original="a.pdf", status="success")
    f.invoice(storage_path=a.StoragePath)
    b = f.attachment(msg, original="po.pdf", status="not_invoice")
    f.rejection(stored_name=b.StoredFileName, decision="pending")
    assert _email(msg).state == o.PENDING


def test_all_processed(ctx):
    msg = f.message(names=("a.pdf", "b.pdf"))
    for name in ("a.pdf", "b.pdf"):
        row = f.attachment(msg, original=name, status="success")
        f.invoice(storage_path=row.StoragePath)
    assert _email(msg).state == o.PROCESSED


def test_only_noise_is_not_invoice(ctx):
    msg = f.message(names=("logo.png",))
    row = f.attachment(msg, original="logo.png", status="irrelevant")
    f.rejection(stored_name=row.StoredFileName, reason="irrelevant", decision="auto_rejected")
    assert _email(msg).state == o.NOT_INVOICE


def test_any_error_wins(ctx):
    msg = f.message(names=("a.pdf", "b.pdf"))
    a = f.attachment(msg, original="a.pdf", status="success")
    f.invoice(storage_path=a.StoragePath)
    f.attachment(msg, original="b.pdf", status="extraction_failed")
    assert _email(msg).state == o.ERROR


@pytest.mark.parametrize("states,expected", [
    ([o.PROCESSED, o.DISMISSED], o.PROCESSED),
    ([o.DISMISSED, o.NOT_INVOICE], o.DISMISSED),
    ([o.IN_PROGRESS, o.PROCESSED], o.IN_PROGRESS),
    ([o.PENDING, o.ERROR], o.ERROR),
])
def test_rollup_precedence(states, expected):
    assert o.rollup(states) == expected


def test_legacy_email_is_sent_to_extraction(ctx):
    old = get_ist_now() - timedelta(days=30)
    msg = f.message(created=old)
    email = _email(msg)
    assert email.state == o.SENT
    assert email.attachments == []


def test_unlinkable_email_is_sent_to_extraction(ctx):
    from vim_database.database import db
    from vim.gmail_tracking.models import GmailIngestReconcile

    msg = f.message()
    db.session.add(GmailIngestReconcile(IngestID=msg.IngestID, State="unlinkable", Attempts=3))
    db.session.commit()
    assert _email(msg).state == o.SENT


def test_recent_unreconciled_email_is_in_progress(ctx):
    msg = f.message()
    assert _email(msg).state == o.IN_PROGRESS


def test_unlinked_names_listed(ctx):
    msg = f.message(names=("a.pdf", "b.pdf"))
    row = f.attachment(msg, original="a.pdf", status="success")
    f.invoice(storage_path=row.StoragePath)
    assert _email(msg).unlinked_names == ["b.pdf"]


# ---- 3.4 batched lookups ----------------------------------------------------

def _count_queries(fn):
    from vim_database.database import db

    statements = []

    def _listener(conn, cursor, statement, *args):
        statements.append(statement)

    engine = db.engine
    event.listen(engine, "before_cursor_execute", _listener)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", _listener)
    return len(statements)


def _seed(n):
    msgs = []
    for i in range(n):
        msg = f.message(names=("a.pdf", "po.pdf"))
        a = f.attachment(msg, original="a.pdf", status="success")
        # Half the invoices are stored under an absolute path to force the
        # secondary lookup.
        prefix = "G:\\abs\\" if i % 2 else "uploads/invoices/"
        f.invoice(storage_path=prefix + a.StoredFileName)
        b = f.attachment(msg, original="po.pdf", status="not_invoice")
        f.rejection(stored_name=b.StoredFileName, decision="pending")
        msgs.append(msg)
    return msgs


def _fresh(msgs):
    """Reload messages the way the dashboard does: one query, attributes loaded."""
    from vim_database.database import db
    from vim_database.models import GmailIngestedMessage

    ids = [m.IngestID for m in msgs]
    db.session.expire_all()
    return GmailIngestedMessage.query.filter(GmailIngestedMessage.IngestID.in_(ids)).all()


def test_query_count_constant(ctx):
    small = _seed(5)
    big = small + _seed(45)
    small_rows = _fresh(small)
    q_small = _count_queries(lambda: o.derive_emails(small_rows))
    big_rows = _fresh(big)
    q_big = _count_queries(lambda: o.derive_emails(big_rows))
    assert q_small == q_big
    assert q_big <= 7


# ---- list_emails -------------------------------------------------------------

def test_list_emails_filters_by_state(ctx):
    pending = f.message(names=("po.pdf",))
    row = f.attachment(pending, original="po.pdf", status="not_invoice")
    f.rejection(stored_name=row.StoredFileName, decision="pending")
    done = f.message()
    row = f.attachment(done, status="success")
    f.invoice(storage_path=row.StoragePath)
    f.message(status="skipped", job_id=None)

    emails, total, counts = o.list_emails(o.PENDING)
    assert [e.message.IngestID for e in emails] == [pending.IngestID]
    assert total == 1
    assert counts[o.PENDING] == 1 and counts[o.PROCESSED] == 1

    emails, total, _ = o.list_emails(None)
    assert total == 2  # skipped emails are not tracked
