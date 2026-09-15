"""Persist rejected uploads for later review or automatic close-out."""

import json
from vim.timezone import get_ist_now

from vim_database.database import db, write_lock
from vim_database.models import RejectedDocument
from vim_logger import get_logger

logger = get_logger("vim.extraction.rejections")

REASON_NOT_INVOICE = "not_invoice"
REASON_NEW_VENDOR = "vendor_not_registered"
REASON_INCOMPLETE_HEADER = "incomplete_header"
REASON_EMPTY = "empty"
REASON_IRRELEVANT = "irrelevant"

DECISION_PENDING = "pending"
DECISION_PROCEEDED = "proceeded"
DECISION_STOPPED = "stopped"
DECISION_AUTO_REJECTED = "auto_rejected"

CATEGORY_AUTO = "auto"
CATEGORY_NOT_INVOICE = "not_invoice"
CATEGORY_NEEDS_REVIEW = "needs_review"

CATEGORY_LABELS = {
    CATEGORY_AUTO: "Empty / irrelevant",
    CATEGORY_NOT_INVOICE: "Not an invoice",
    CATEGORY_NEEDS_REVIEW: "Needs a person",
}

CATEGORY_HINTS = {
    CATEGORY_AUTO: "Blank or unrelated files. Rejected automatically — no review queue.",
    CATEGORY_NOT_INVOICE: "Business documents that are not vendor invoices (PO, quote, bill of supply, receipt).",
    CATEGORY_NEEDS_REVIEW: "Looks like an invoice but the vendor is missing or not registered.",
}

REASON_LABELS = {
    REASON_EMPTY: "Empty document",
    REASON_IRRELEVANT: "Irrelevant to invoices",
    REASON_NOT_INVOICE: "Not an invoice",
    REASON_NEW_VENDOR: "Unknown vendor",
    REASON_INCOMPLETE_HEADER: "Missing vendor name",
}

DECISION_LABELS = {
    DECISION_PENDING: "Pending",
    DECISION_PROCEEDED: "Proceeded",
    DECISION_STOPPED: "Stopped",
    DECISION_AUTO_REJECTED: "Rejected automatically",
}

_PREVIEW_CHARS = 20000

_SLIM_SKIP = {"raw_text", "file_path"}


def _fit(value, length):
    if value in (None, ""):
        return None
    return str(value).strip()[:length] or None


def _slim(record: dict) -> dict:
    payload = {k: v for k, v in record.items() if k not in _SLIM_SKIP}
    return json.loads(json.dumps(payload, default=str))


def categorize_rejection(row) -> str:
    reason = (getattr(row, "Reason", None) or "").strip()
    decision = (getattr(row, "Decision", None) or "").strip()
    if reason in (REASON_EMPTY, REASON_IRRELEVANT) or decision == DECISION_AUTO_REJECTED:
        return CATEGORY_AUTO
    if reason in (REASON_NEW_VENDOR, REASON_INCOMPLETE_HEADER):
        return CATEGORY_NEEDS_REVIEW
    return CATEGORY_NOT_INVOICE


def reason_label(reason: str) -> str:
    return REASON_LABELS.get(reason, reason or "—")


def decision_label(decision: str) -> str:
    return DECISION_LABELS.get(decision, decision or "—")


def record_rejection(record: dict, *, reason: str, decision: str | None = None) -> RejectedDocument:
    """
    Insert or update a rejected_document row for this upload.

    One row per stored file name, so a document that is first held as
    not-an-invoice and later as an unknown vendor keeps a single history.
    """
    with write_lock:
        stored = record.get("stored_file_name") or ""
        logger.info("[REJECT] Recording rejection: stored='%s' reason='%s'", stored, reason)
        row = RejectedDocument.query.filter_by(StoredFileName=stored).first()
        if row is None:
            logger.debug("[REJECT] Creating new RejectedDocument row for '%s'", stored)
            row = RejectedDocument(
                StoredFileName=stored,
                FileName=record.get("file_name") or stored,
                Decision=decision or DECISION_PENDING,
            )
            db.session.add(row)
        else:
            logger.debug("[REJECT] Updating existing RejectedDocument row for '%s'", stored)

        row.Reason = reason
        chosen = decision or DECISION_PENDING
        if chosen == DECISION_AUTO_REJECTED:
            row.Decision = DECISION_AUTO_REJECTED
            row.DecidedDate = get_ist_now()
            row.DecidedByUserID = None
        elif row.Decision not in (DECISION_STOPPED, DECISION_AUTO_REJECTED):
            row.Decision = DECISION_PENDING
            row.DecidedDate = None
            row.DecidedByUserID = None

        row.FileName = record.get("file_name") or row.FileName or stored
        row.StoragePath = record.get("file_path")
        row.DocumentType = _fit(
            record.get("_document_type") or record.get("document_type"), 100
        )
        row.ClassifierReason = _fit(
            record.get("_incomplete_reason") or record.get("_not_invoice_reason"),
            500,
        )
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
    with write_lock:
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
