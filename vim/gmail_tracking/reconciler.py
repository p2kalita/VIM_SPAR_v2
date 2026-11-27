"""
Background reconciler: link Gmail-ingested emails to their per-attachment
pipeline outcomes.

Reads gmail_ingested_message and the in-memory extraction jobs; writes only
the two gmail_tracking tables. Exact capture uses the job while it is still in
memory. When the job is gone (pruned after an hour, or lost on restart) a
fallback matches by original file name and time, and links only when exactly
one candidate exists, so an attachment is never attributed to the wrong file.
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from datetime import timedelta

from sqlalchemy import and_

from vim.gmail_tracking import settings
from vim.gmail_tracking.models import (
    LINK_FALLBACK,
    LINK_JOB,
    RECONCILE_LINKED,
    RECONCILE_UNLINKABLE,
    RECONCILE_WAITING,
    GmailIngestedAttachment,
    GmailIngestReconcile,
)
from vim.gmail_tracking.outcomes import file_name_of
from vim.timezone import get_ist_now
from vim_logger import get_logger

logger = get_logger("vim.gmail_tracking")

# Emails examined per cycle; the rest wait for the next one.
_BATCH = 200

_started = False
_start_lock = threading.Lock()
_cycle_lock = threading.Lock()


# --------------------------------------------------------------------------
# Candidate selection
# --------------------------------------------------------------------------
def candidates(now=None, limit: int = _BATCH):
    """Ingested emails inside the lookback window that are not settled yet."""
    from vim_database.models import GmailIngestedMessage

    now = now or get_ist_now()
    cutoff = now - timedelta(days=settings.lookback_days())
    settled = (RECONCILE_LINKED, RECONCILE_UNLINKABLE)
    return (
        GmailIngestedMessage.query
        .outerjoin(
            GmailIngestReconcile,
            GmailIngestReconcile.IngestID == GmailIngestedMessage.IngestID,
        )
        .filter(
            GmailIngestedMessage.Status == "ingested",
            GmailIngestedMessage.JobID.isnot(None),
            GmailIngestedMessage.JobID != "",
            GmailIngestedMessage.CreatedDate >= cutoff,
            (GmailIngestReconcile.IngestID.is_(None))
            | (GmailIngestReconcile.State.notin_(settled)),
        )
        .order_by(GmailIngestedMessage.CreatedDate.asc(), GmailIngestedMessage.IngestID.asc())
        .limit(limit)
        .all()
    )


# --------------------------------------------------------------------------
# Exact capture from the in-memory job
# --------------------------------------------------------------------------
def _record_detail(record: dict) -> str | None:
    detail = (
        record.get("_extraction_error")
        or record.get("_db_error")
        or record.get("_incomplete_reason")
        or record.get("_not_invoice_reason")
    )
    return str(detail)[:500] if detail else None


def rows_from_job(job: dict) -> list[dict]:
    """Attachment row values for every file in a finished job."""
    rows = []
    result_names = Counter()
    for record in job.get("results") or []:
        original = str(record.get("file_name") or "").strip()
        path = str(record.get("file_path") or "").strip() or None
        stored = str(record.get("stored_file_name") or "").strip() or file_name_of(path) or None
        invoice_id = record.get("invoice_id")
        result_names[original] += 1
        rows.append({
            "OriginalName": original or (stored or "attachment"),
            "StoredFileName": stored,
            "StoragePath": path,
            "CapturedStatus": (str(record.get("status") or "").strip() or None),
            "CapturedDetail": _record_detail(record),
            "CapturedInvoiceID": int(invoice_id) if invoice_id is not None else None,
        })

    # A worker that raised leaves a file entry with status "error" and no
    # result record, so its stored file is unknown.
    for entry in job.get("files") or []:
        if entry.get("status") != "error":
            continue
        name = str(entry.get("file_name") or "").strip()
        if result_names[name] > 0:
            result_names[name] -= 1
            continue
        rows.append({
            "OriginalName": name or "attachment",
            "StoredFileName": None,
            "StoragePath": None,
            "CapturedStatus": "error",
            "CapturedDetail": (str(entry.get("detail") or "") or "Extraction failed.")[:500],
            "CapturedInvoiceID": None,
        })
    return rows


def _existing_rows(ingest_id: int) -> list[GmailIngestedAttachment]:
    return GmailIngestedAttachment.query.filter_by(IngestID=ingest_id).all()


def _insert_missing(ingest_id: int, values: list[dict], method: str) -> int:
    """Insert rows not already present; return how many were added."""
    existing = _existing_rows(ingest_id)
    stored = {r.StoredFileName for r in existing if r.StoredFileName}
    unknown = Counter(r.OriginalName for r in existing if not r.StoredFileName)
    added = 0
    from vim_database.database import db

    for value in values:
        if value["StoredFileName"]:
            if value["StoredFileName"] in stored:
                continue
            stored.add(value["StoredFileName"])
        else:
            if unknown[value["OriginalName"]] > 0:
                unknown[value["OriginalName"]] -= 1
                continue
        db.session.add(GmailIngestedAttachment(IngestID=ingest_id, LinkMethod=method, **value))
        added += 1
    return added


def _reconcile_row(ingest_id: int) -> GmailIngestReconcile:
    from vim_database.database import db

    row = db.session.get(GmailIngestReconcile, ingest_id)
    if row is None:
        row = GmailIngestReconcile(IngestID=ingest_id, State=RECONCILE_WAITING, Attempts=0)
        db.session.add(row)
    return row


def _set_state(ingest_id: int, *, state: str, method: str | None = None,
               bump_attempts: bool = False) -> GmailIngestReconcile:
    row = _reconcile_row(ingest_id)
    row.State = state
    if method:
        row.Method = method
    if bump_attempts:
        row.Attempts = (row.Attempts or 0) + 1
    row.LastCheckedAt = get_ist_now()
    return row


# --------------------------------------------------------------------------
# Fallback linking when the job is gone
# --------------------------------------------------------------------------
def _claimed_stored_names(names: set[str]) -> set[str]:
    if not names:
        return set()
    return {
        stored for (stored,) in
        GmailIngestedAttachment.query
        .with_entities(GmailIngestedAttachment.StoredFileName)
        .filter(GmailIngestedAttachment.StoredFileName.in_(names))
    }


def fallback_values(message, now=None) -> list[dict]:
    """Rows for attachment names with exactly one unclaimed match in the window."""
    from vim_database.models import InvoiceDocument, RejectedDocument

    names = [n for n in (p.strip() for p in (message.AttachmentNames or "").split(", ")) if n]
    if not names or message.CreatedDate is None:
        return []

    start = message.CreatedDate - timedelta(minutes=settings.MATCH_GRACE_MINUTES)
    end = message.CreatedDate + timedelta(hours=settings.match_hours())

    already = {r.OriginalName for r in _existing_rows(message.IngestID)}
    counts = Counter(names)
    values = []
    for name in counts:
        if name in already:
            continue
        found: dict[str, dict] = {}
        for doc in InvoiceDocument.query.filter(
            InvoiceDocument.FileName == name,
            and_(InvoiceDocument.UploadDate >= start, InvoiceDocument.UploadDate <= end),
        ):
            stored = file_name_of(doc.StoragePath)
            if stored:
                found.setdefault(stored, {"StoragePath": doc.StoragePath, "CapturedInvoiceID": doc.InvoiceID})
        for rej in RejectedDocument.query.filter(
            RejectedDocument.FileName == name,
            and_(RejectedDocument.CreatedDate >= start, RejectedDocument.CreatedDate <= end),
        ):
            found.setdefault(rej.StoredFileName, {"StoragePath": rej.StoragePath, "CapturedInvoiceID": None})

        claimed = _claimed_stored_names(set(found))
        claimed |= {v["StoredFileName"] for v in values}
        unclaimed = {k: v for k, v in found.items() if k not in claimed}

        # The same name twice in one email cannot be told apart either.
        if counts[name] != 1 or len(unclaimed) != 1:
            if unclaimed:
                logger.info(
                    "[GMAIL-TRACKING] ingest=%s '%s': %d candidates, not linking",
                    message.IngestID, name, len(unclaimed),
                )
            continue
        ((stored, info),) = unclaimed.items()
        values.append({
            "OriginalName": name,
            "StoredFileName": stored,
            "StoragePath": info["StoragePath"],
            "CapturedStatus": None,
            "CapturedDetail": None,
            "CapturedInvoiceID": info["CapturedInvoiceID"],
        })
    return values


# --------------------------------------------------------------------------
# One email, one cycle
# --------------------------------------------------------------------------
def reconcile_message(message, now=None) -> str:
    """Advance one email. Returns its reconcile state afterwards."""
    from vim.extraction import jobs
    from vim_database.database import db, write_lock

    job = jobs.get_job(message.JobID)

    if job is not None and job.get("status") != "complete":
        with write_lock:
            _set_state(message.IngestID, state=RECONCILE_WAITING)
            db.session.commit()
        return RECONCILE_WAITING

    if job is not None:
        values = rows_from_job(job)
        with write_lock:
            added = _insert_missing(message.IngestID, values, LINK_JOB)
            _set_state(message.IngestID, state=RECONCILE_LINKED, method=LINK_JOB)
            db.session.commit()
        logger.info(
            "[GMAIL-TRACKING] ingest=%s linked from job (%d attachment(s), %d new)",
            message.IngestID, len(values), added,
        )
        return RECONCILE_LINKED

    values = fallback_values(message, now=now)
    with write_lock:
        added = _insert_missing(message.IngestID, values, LINK_FALLBACK)
        if added or _existing_rows(message.IngestID):
            row = _set_state(message.IngestID, state=RECONCILE_LINKED, method=LINK_FALLBACK)
        else:
            row = _set_state(message.IngestID, state=RECONCILE_WAITING, bump_attempts=True)
            if row.Attempts >= settings.MAX_FALLBACK_ATTEMPTS:
                row.State = RECONCILE_UNLINKABLE
        state = row.State
        db.session.commit()

    if state == RECONCILE_UNLINKABLE:
        logger.info("[GMAIL-TRACKING] ingest=%s job gone and no unique match — unlinkable",
                    message.IngestID)
    elif state == RECONCILE_LINKED:
        logger.info("[GMAIL-TRACKING] ingest=%s linked by fallback (%d attachment(s))",
                    message.IngestID, added)
    return state


def reconcile_once(app) -> dict:
    """Run one cycle. Never raises; returns a summary for logs and tests."""
    summary = {"checked": 0, "linked": 0, "waiting": 0, "unlinkable": 0, "errors": 0}
    if not _cycle_lock.acquire(blocking=False):
        summary["detail"] = "cycle already running"
        return summary
    try:
        with app.app_context():
            from vim_database.database import db

            try:
                now = get_ist_now()
                for message in candidates(now=now):
                    summary["checked"] += 1
                    try:
                        state = reconcile_message(message, now=now)
                        summary[state] = summary.get(state, 0) + 1
                    except Exception:
                        db.session.rollback()
                        summary["errors"] += 1
                        logger.exception("[GMAIL-TRACKING] ingest=%s failed", message.IngestID)
            except Exception:
                db.session.rollback()
                summary["errors"] += 1
                logger.exception("[GMAIL-TRACKING] cycle failed")
            finally:
                db.session.remove()
    finally:
        _cycle_lock.release()
    return summary


# --------------------------------------------------------------------------
# Thread lifecycle
# --------------------------------------------------------------------------
def start_reconciler(app) -> bool:
    """Start the background thread once per process. Returns True if started."""
    global _started
    if not settings.tracking_enabled():
        logger.info("[GMAIL-TRACKING] Reconciler disabled by GMAIL_TRACKING")
        return False
    with _start_lock:
        if _started:
            return False
        _started = True
    thread = threading.Thread(target=_loop, args=(app,), name="gmail-tracking", daemon=True)
    thread.start()
    logger.info("[GMAIL-TRACKING] Reconciler started (every %ss)", settings.poll_seconds())
    return True


def _loop(app) -> None:
    while True:
        # Sleep first so create_app() finishes db.create_all() before any query.
        time.sleep(settings.poll_seconds())
        try:
            summary = reconcile_once(app)
            if summary.get("checked"):
                logger.info(
                    "[GMAIL-TRACKING] cycle checked=%s linked=%s waiting=%s unlinkable=%s errors=%s",
                    summary["checked"], summary.get("linked", 0), summary.get("waiting", 0),
                    summary.get("unlinkable", 0), summary["errors"],
                )
        except Exception:
            logger.exception("[GMAIL-TRACKING] reconciler loop error")
