"""
Derive attachment and email outcome states at read time.

Nothing here writes. State comes from the current invoice_document and
rejected_document rows each time it is asked for, so a decision an admin
makes later (proceeding or stopping a held document) shows up with no hook
into the routes that make that decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import PurePath

from sqlalchemy import or_

from vim.gmail_tracking import settings
from vim.gmail_tracking.models import (
    CAPTURE_ERROR_STATUSES,
    RECONCILE_UNLINKABLE,
    GmailIngestedAttachment,
    GmailIngestReconcile,
)
from vim.timezone import get_ist_now

PROCESSED = "processed"
PENDING = "pending"
IN_PROGRESS = "in_progress"
NOT_INVOICE = "not_invoice"
DISMISSED = "dismissed"
ERROR = "error"
SENT = "sent_to_extraction"

LABELS = {
    PROCESSED: "Processed",
    PENDING: "Pending",
    IN_PROGRESS: "In progress",
    NOT_INVOICE: "Not invoice",
    DISMISSED: "Dismissed",
    ERROR: "Error",
    SENT: "Sent to extraction",
}

# Email rollup precedence: the first state any attachment is in wins.
ROLLUP_ORDER = (ERROR, PENDING, IN_PROGRESS, PROCESSED, DISMISSED, NOT_INVOICE)

# States an admin can filter the dashboard by, in display order.
FILTER_STATES = (PENDING, PROCESSED, IN_PROGRESS, NOT_INVOICE, DISMISSED, ERROR, SENT)

_AUTO_REJECT_STATUSES = ("empty", "irrelevant")

# SQLite builds a left-deep tree for chained ORs; keep each chain short.
_OR_CHUNK = 200


@dataclass
class AttachmentOutcome:
    attachment: GmailIngestedAttachment
    state: str
    invoice_id: int | None = None
    rejection_id: int | None = None
    detail: str | None = None
    document_type: str | None = None

    @property
    def label(self) -> str:
        return LABELS[self.state]


@dataclass
class EmailOutcome:
    message: object
    state: str
    attachments: list[AttachmentOutcome] = field(default_factory=list)
    unlinked_names: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return LABELS[self.state]


def file_name_of(path: str | None) -> str:
    """Last path component, for either separator style."""
    if not path:
        return ""
    return PurePath(path.replace("\\", "/")).name


def rollup(states) -> str:
    present = set(states)
    for state in ROLLUP_ORDER:
        if state in present:
            return state
    return IN_PROGRESS


def _invoice_documents_for(rows):
    """Map stored file name -> InvoiceDocument, using at most a few queries."""
    from vim_database.models import InvoiceDocument

    by_name: dict[str, object] = {}
    paths = {r.StoragePath for r in rows if r.StoragePath}
    if paths:
        for doc in InvoiceDocument.query.filter(InvoiceDocument.StoragePath.in_(paths)):
            by_name.setdefault(file_name_of(doc.StoragePath), doc)

    # The same file can be stored under a different path string, e.g. a
    # force-processed document uses the resolved absolute upload path.
    missing = sorted({
        r.StoredFileName for r in rows
        if r.StoredFileName and r.StoredFileName not in by_name
    })
    for start in range(0, len(missing), _OR_CHUNK):
        chunk = missing[start:start + _OR_CHUNK]
        clauses = []
        for name in chunk:
            clauses.append(InvoiceDocument.StoragePath.endswith("/" + name, autoescape=True))
            clauses.append(InvoiceDocument.StoragePath.endswith("\\" + name, autoescape=True))
            clauses.append(InvoiceDocument.StoragePath == name)
        for doc in InvoiceDocument.query.filter(or_(*clauses)):
            by_name.setdefault(file_name_of(doc.StoragePath), doc)
    return by_name


def _rejections_for(rows):
    from vim_database.models import RejectedDocument

    names = {r.StoredFileName for r in rows if r.StoredFileName}
    if not names:
        return {}
    return {
        rej.StoredFileName: rej
        for rej in RejectedDocument.query.filter(RejectedDocument.StoredFileName.in_(names))
    }


def _existing_invoice_ids(rows) -> set[int]:
    from vim_database.models import Invoice

    ids = {r.CapturedInvoiceID for r in rows if r.CapturedInvoiceID is not None}
    if not ids:
        return set()
    return {
        inv_id for (inv_id,) in
        Invoice.query.with_entities(Invoice.InvoiceID).filter(Invoice.InvoiceID.in_(ids))
    }


def _state_from_rejection(rej) -> str:
    from vim.extraction.rejections import (
        DECISION_AUTO_REJECTED,
        DECISION_STOPPED,
    )

    decision = (rej.Decision or "").strip()
    if decision == DECISION_STOPPED:
        return DISMISSED
    if decision == DECISION_AUTO_REJECTED:
        return NOT_INVOICE
    # pending, or proceeded without an invoice yet: still needs a person.
    return PENDING


def derive_attachments(rows) -> list[AttachmentOutcome]:
    """State for each tracked attachment, first match wins (design Decision 5)."""
    rows = list(rows)
    if not rows:
        return []
    invoices = _invoice_documents_for(rows)
    rejections = _rejections_for(rows)
    live_invoice_ids = _existing_invoice_ids(rows)

    out: list[AttachmentOutcome] = []
    for row in rows:
        name = row.StoredFileName
        doc = invoices.get(name) if name else None
        if doc is not None:
            out.append(AttachmentOutcome(row, PROCESSED, invoice_id=doc.InvoiceID))
            continue

        rej = rejections.get(name) if name else None
        if rej is not None:
            out.append(AttachmentOutcome(
                row,
                _state_from_rejection(rej),
                rejection_id=rej.RejectionID,
                detail=rej.ClassifierReason,
                document_type=rej.DocumentType,
            ))
            continue

        if row.CapturedInvoiceID is not None:
            if row.CapturedInvoiceID in live_invoice_ids:
                out.append(AttachmentOutcome(row, PROCESSED, invoice_id=row.CapturedInvoiceID))
            else:
                out.append(AttachmentOutcome(
                    row, ERROR,
                    detail=f"Invoice {row.CapturedInvoiceID} no longer exists.",
                ))
            continue

        status = (row.CapturedStatus or "").strip()
        if status in CAPTURE_ERROR_STATUSES:
            out.append(AttachmentOutcome(row, ERROR, detail=row.CapturedDetail))
        elif status in _AUTO_REJECT_STATUSES:
            out.append(AttachmentOutcome(row, NOT_INVOICE, detail=row.CapturedDetail))
        elif status:
            # Finished in the pipeline, yet neither table has a record of it.
            out.append(AttachmentOutcome(
                row, ERROR,
                detail=row.CapturedDetail
                or f"No invoice or rejected-document record found (status: {status}).",
            ))
        else:
            out.append(AttachmentOutcome(row, IN_PROGRESS))
    return out


def _attachment_names(message) -> list[str]:
    raw = (getattr(message, "AttachmentNames", None) or "").strip()
    return [n for n in (part.strip() for part in raw.split(", ")) if n]


def _unlinked(message, outcomes: list[AttachmentOutcome]) -> list[str]:
    remaining = [o.attachment.OriginalName for o in outcomes]
    unlinked = []
    for name in _attachment_names(message):
        if name in remaining:
            remaining.remove(name)
        else:
            unlinked.append(name)
    return unlinked


def derive_emails(messages, *, now=None) -> list[EmailOutcome]:
    """Outcome for each ingested email, in the order given."""
    messages = list(messages)
    if not messages:
        return []
    now = now or get_ist_now()
    ids = [m.IngestID for m in messages]

    rows = (
        GmailIngestedAttachment.query
        .filter(GmailIngestedAttachment.IngestID.in_(ids))
        .order_by(GmailIngestedAttachment.AttachmentID.asc())
        .all()
    )
    reconcile = {
        r.IngestID: r
        for r in GmailIngestReconcile.query.filter(GmailIngestReconcile.IngestID.in_(ids))
    }
    by_message: dict[int, list[AttachmentOutcome]] = {i: [] for i in ids}
    for outcome in derive_attachments(rows):
        by_message[outcome.attachment.IngestID].append(outcome)

    cutoff = now - timedelta(days=settings.lookback_days())
    result = []
    for message in messages:
        outcomes = by_message[message.IngestID]
        if outcomes:
            state = rollup(o.state for o in outcomes)
        else:
            # Nothing linked: either still being reconciled, or given up on
            # (fallback found nothing, or the email predates tracking).
            rec = reconcile.get(message.IngestID)
            gave_up = rec is not None and rec.State == RECONCILE_UNLINKABLE
            too_old = message.CreatedDate is not None and message.CreatedDate < cutoff
            state = SENT if (gave_up or too_old) else IN_PROGRESS
        result.append(EmailOutcome(
            message=message,
            state=state,
            attachments=outcomes,
            unlinked_names=_unlinked(message, outcomes),
        ))
    return result


# Newest ingested emails considered when filtering by status.
MAX_FILTER_SCAN = 2000


def _ingested_query():
    from vim_database.models import GmailIngestedMessage

    return (
        GmailIngestedMessage.query
        .filter(GmailIngestedMessage.Status == "ingested")
        .order_by(GmailIngestedMessage.CreatedDate.desc(), GmailIngestedMessage.IngestID.desc())
    )


def list_emails(status: str | None = None, page: int = 1, per_page: int = 50):
    """
    One dashboard page of ingested emails with outcomes.

    Returns (emails, total, counts). Unfiltered pages paginate in SQL; a
    status filter derives state for the newest MAX_FILTER_SCAN emails, since
    state is computed, not stored.
    """
    page = max(1, int(page or 1))
    status = status if status in FILTER_STATES else None
    scanned = derive_emails(_ingested_query().limit(MAX_FILTER_SCAN).all())

    counts = {s: 0 for s in FILTER_STATES}
    for email in scanned:
        counts[email.state] = counts.get(email.state, 0) + 1

    start = (page - 1) * per_page
    if status is None:
        query = _ingested_query()
        total = query.count()
        if start + per_page <= len(scanned) or len(scanned) < MAX_FILTER_SCAN:
            return scanned[start:start + per_page], total, counts
        page_rows = query.offset(start).limit(per_page).all()
        return derive_emails(page_rows), total, counts

    matching = [e for e in scanned if e.state == status]
    return matching[start:start + per_page], len(matching), counts
