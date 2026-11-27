"""
Tables for Gmail ingest outcome tracking.

Both tables are new. Nothing here alters gmail_ingested_message or any other
existing table; they reference it by foreign key only.
"""

from vim.timezone import get_ist_now
from vim_database.database import db

# Pipeline statuses captured from the extraction job for a single attachment.
CAPTURE_ERROR_STATUSES = ("extraction_failed", "db_error", "error")

LINK_JOB = "job"
LINK_FALLBACK = "fallback"

RECONCILE_WAITING = "waiting"
RECONCILE_LINKED = "linked"
RECONCILE_UNLINKABLE = "unlinkable"


class GmailIngestedAttachment(db.Model):
    """One attachment that Gmail Ingest sent to extraction, and how it ended."""

    __tablename__ = "gmail_ingested_attachment"
    __table_args__ = (
        db.UniqueConstraint(
            "IngestID", "StoredFileName", name="uq_gmail_attachment_ingest_stored"
        ),
    )

    AttachmentID = db.Column(db.Integer, primary_key=True)
    IngestID = db.Column(
        db.Integer,
        db.ForeignKey("gmail_ingested_message.IngestID"),
        nullable=False,
        index=True,
    )
    OriginalName = db.Column(db.String(255), nullable=False)
    # Null only for a file that errored before the pipeline produced a record.
    StoredFileName = db.Column(db.String(255), index=True)
    StoragePath = db.Column(db.String(500))
    CapturedStatus = db.Column(db.String(40))
    CapturedDetail = db.Column(db.String(500))
    CapturedInvoiceID = db.Column(db.Integer)
    LinkMethod = db.Column(db.String(20), nullable=False, default=LINK_JOB)
    CreatedDate = db.Column(db.DateTime, default=get_ist_now)

    def __repr__(self):
        return f"<GmailIngestedAttachment {self.IngestID}:{self.StoredFileName or self.OriginalName}>"


class GmailIngestReconcile(db.Model):
    """Per-email bookkeeping for the reconciler."""

    __tablename__ = "gmail_ingest_reconcile"

    IngestID = db.Column(
        db.Integer,
        db.ForeignKey("gmail_ingested_message.IngestID"),
        primary_key=True,
    )
    State = db.Column(db.String(20), nullable=False, default=RECONCILE_WAITING)
    Method = db.Column(db.String(20))
    Attempts = db.Column(db.Integer, nullable=False, default=0)
    LastCheckedAt = db.Column(db.DateTime)

    def __repr__(self):
        return f"<GmailIngestReconcile {self.IngestID} {self.State}>"
