"""Persist rejected uploads (not-an-invoice, unknown vendor) for later review."""

import json
from datetime import datetime
from vim.timezone import get_ist_now

from vim_database.database import db
from vim_database.models import RejectedDocument
from vim_logger import get_logger

logger = get_logger("vim.extraction.rejections")

REASON_NOT_INVOICE = "not_invoice"
REASON_NEW_VENDOR = "vendor_not_registered"

DECISION_PENDING = "pending"
DECISION_PROCEEDED = "proceeded"
DECISION_STOPPED = "stopped"

_PREVIEW_CHARS = 20000

_SLIM_SKIP = {"raw_text", "file_path"}


def _fit(value, length):
    if value in (None, ""):
        return None
    return str(value).strip()[:length] or None


def _slim(record: dict) -> dict:
    payload = {k: v for k, v in record.items() if k not in _SLIM_SKIP}
    return json.loads(json.dumps(payload, default=str))


def record_rejection(record: dict, *, reason: str) -> RejectedDocument:
    """
    Insert or update a rejected_document row for this upload.

    One row per stored file name, so a document that is first held as
    not-an-invoice and later as an unknown vendor keeps a single history.
    """
    stored = record.get("stored_file_name") or ""
    logger.info("[REJECT] Recording rejection: stored='%s' reason='%s'", stored, reason)
    row = RejectedDocument.query.filter_by(StoredFileName=stored).first()
    if row is None:
        logger.debug("[REJECT] Creating new RejectedDocument row for '%s'", stored)
        row = RejectedDocument(
            StoredFileName=stored,
            FileName=record.get("file_name") or stored,
            Decision=DECISION_PENDING,
        )
        db.session.add(row)
    else:
        logger.debug("[REJECT] Updating existing RejectedDocument row for '%s'", stored)

    row.Reason = reason
    if row.Decision != DECISION_STOPPED:
        row.Decision = DECISION_PENDING
        row.DecidedDate = None
        row.DecidedByUserID = None

    row.FileName = record.get("file_name") or row.FileName or stored
    row.StoragePath = record.get("file_path")
    row.DocumentType = _fit(
        record.get("_document_type") or record.get("document_type"), 100
    )
    row.ClassifierReason = _fit(record.get("_not_invoice_reason"), 500)
    row.VendorName = _fit(record.get("vendor_name"), 100)
    row.InvoiceNumber = _fit(record.get("invoice_number"), 50)
    amount = record.get("total_due")
    row.InvoiceAmount = amount if isinstance(amount, (int, float)) else None
    row.Currency = _fit(record.get("currency"), 10)
    row.ExtractedJson = _slim(record)
    row.RawTextPreview = (record.get("raw_text") or "")[:_PREVIEW_CHARS] or None

    db.session.commit()
    logger.debug("[REJECT] Committed rejection row for '%s'", stored)
    return row


def mark_decision(stored_file_name: str, decision: str, user_id=None) -> RejectedDocument | None:
    """Record the admin's proceed/stop choice on an existing rejection row."""
    logger.info("[REJECT] mark_decision: stored='%s' decision='%s' user_id=%s",
                stored_file_name, decision, user_id)
    row = RejectedDocument.query.filter_by(StoredFileName=stored_file_name).first()
    if row is None:
        logger.debug("[REJECT] No rejection row found for '%s'", stored_file_name)
        return None
    row.Decision = decision
    row.DecidedDate = get_ist_now()
    row.DecidedByUserID = user_id
    db.session.commit()
    logger.debug("[REJECT] Decision saved for '%s'", stored_file_name)
    return row
