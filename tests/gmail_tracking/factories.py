"""Small row builders for gmail_tracking tests. Call inside an app context."""

from __future__ import annotations

import itertools
from datetime import date, timedelta

from vim.timezone import get_ist_now

_seq = itertools.count(1)

UPLOAD_DIR = "uploads/invoices"


def stored(name: str) -> str:
    return f"{next(_seq):032x}_{name}"


def message(*, names=("invoice.pdf",), status="ingested", job_id="job-1", created=None):
    from vim_database.database import db
    from vim_database.models import GmailIngestedMessage

    msg = GmailIngestedMessage(
        GmailMessageId=f"gm-{next(_seq)}",
        FromAddress="ap@vendor.test",
        Subject="Invoice",
        AttachmentNames=", ".join(names) or None,
        JobID=job_id,
        Status=status,
    )
    db.session.add(msg)
    db.session.flush()
    msg.CreatedDate = created or get_ist_now()
    db.session.commit()
    return msg


def attachment(msg, *, original="invoice.pdf", stored_name=None, path=None,
               status=None, detail=None, invoice_id=None, method="job"):
    from vim_database.database import db
    from vim.gmail_tracking.models import GmailIngestedAttachment

    stored_name = stored_name if stored_name is not None else stored(original)
    row = GmailIngestedAttachment(
        IngestID=msg.IngestID,
        OriginalName=original,
        StoredFileName=stored_name or None,
        StoragePath=path if path is not None else (f"{UPLOAD_DIR}/{stored_name}" if stored_name else None),
        CapturedStatus=status,
        CapturedDetail=detail,
        CapturedInvoiceID=invoice_id,
        LinkMethod=method,
    )
    db.session.add(row)
    db.session.commit()
    return row


def invoice(*, storage_path=None, file_name="invoice.pdf", uploaded=None):
    from vim_database.database import db
    from vim_database.models import Invoice, InvoiceDocument

    inv = Invoice(
        InvoiceNumber=f"INV-{next(_seq)}",
        InvoiceDate=date.today(),
        VendorID=1,
        PONumber="PO-1",
        InvoiceAmount=100,
        Currency="INR",
        DueDate=date.today() + timedelta(days=30),
        InvoiceStatus="received",
    )
    db.session.add(inv)
    db.session.flush()
    if storage_path is not None:
        doc = InvoiceDocument(
            InvoiceID=inv.InvoiceID,
            FileName=file_name,
            FileType="PDF",
            StoragePath=storage_path,
        )
        db.session.add(doc)
        db.session.flush()
        if uploaded is not None:
            doc.UploadDate = uploaded
    db.session.commit()
    return inv


def rejection(*, stored_name, file_name="invoice.pdf", reason="not_invoice",
              decision="pending", created=None):
    from vim_database.database import db
    from vim_database.models import RejectedDocument

    rej = RejectedDocument(
        Reason=reason,
        Decision=decision,
        FileName=file_name,
        StoredFileName=stored_name,
        StoragePath=f"{UPLOAD_DIR}/{stored_name}",
        DocumentType="Purchase Order" if reason == "not_invoice" else None,
        ClassifierReason="Looks like a PO.",
    )
    db.session.add(rej)
    db.session.flush()
    if created is not None:
        rej.CreatedDate = created
    db.session.commit()
    return rej
