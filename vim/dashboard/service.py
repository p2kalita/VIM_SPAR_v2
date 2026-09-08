from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import joinedload

from vim.timezone import get_ist_now
from vim_database.database import db
from vim_logger import get_logger
from vim_database.models import (
    Approval,
    Invoice,
    RejectedDocument,
    User,
    Vendor,
)

logger = get_logger("vim.dashboard.service")

_MIN_REAL_DATE = date(2000, 1, 1)
_CLOSED_STATUSES = ("rejected",)
_STATUS_ORDER = ("Pending Approval", "Approved", "Rejected")
_PENDING_STATUSES = ("pending approval", "pending", "overdue")
_APPROVED_STATUSES = ("approved", "paid")
_REJECTED_STATUSES = ("rejected", "failed")
_VIEWS = {
    "all", "pending", "overdue", "held", "month", "week",
    "approved", "rejected", "vendor", "approver",
}


def _canonical_status(status):
    key = (status or "").strip().lower()
    if key in _APPROVED_STATUSES:
        return "Approved"
    if key in _REJECTED_STATUSES:
        return "Rejected"
    if key in _PENDING_STATUSES:
        return "Pending Approval"
    if not status:
        return "Other"
    return str(status).strip().title()


def _decimal_to_float(value):
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def _is_open_invoice():
    return ~func.lower(Invoice.InvoiceStatus).in_(_CLOSED_STATUSES)


def _days_open(start, now):
    if start is None:
        return 0
    if getattr(start, "tzinfo", None):
        start = start.replace(tzinfo=None)
    now_naive = now.replace(tzinfo=None) if getattr(now, "tzinfo", None) else now
    return max((now_naive - start).days, 0)


def _fmt_date(value):
    if not value or value < _MIN_REAL_DATE:
        return "—"
    return value.strftime("%d-%b-%Y")


def _serialize_invoice(inv):
    return {
        "invoice_id": inv.InvoiceID,
        "invoice_number": inv.InvoiceNumber or f"INV-{inv.InvoiceID}",
        "vendor": inv.vendor.VendorName if inv.vendor else "—",
        "status": _canonical_status(inv.InvoiceStatus),
        "amount": _decimal_to_float(inv.InvoiceAmount),
        "currency": inv.Currency or "",
        "invoice_date": _fmt_date(inv.InvoiceDate),
        "due_date": _fmt_date(inv.DueDate),
    }


def _status_counts_from_rows(status_rows):
    raw_counts = {}
    for status, count in status_rows:
        label = _canonical_status(status)
        raw_counts[label] = raw_counts.get(label, 0) + count
    status_counts = {
        name: raw_counts[name]
        for name in _STATUS_ORDER
        if name in raw_counts
    }
    for name, count in raw_counts.items():
        status_counts.setdefault(name, count)
    return status_counts


def _due_bucket(inv, today):
    days = (today - inv.DueDate).days
    if days > 0:
        when = f"{days} day{'s' if days != 1 else ''} overdue"
        bucket = "overdue"
    elif days == 0:
        when = "Due today"
        bucket = "today"
    else:
        left = -days
        when = f"in {left} day{'s' if left != 1 else ''}"
        bucket = "upcoming"
    return when, bucket


def _apply_view(query, view, now, vendor_id, approver):
    today = now.date()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    week_start = now - timedelta(days=7)
    real_due = Invoice.DueDate >= _MIN_REAL_DATE
    open_invoices = _is_open_invoice()

    if view == "pending":
        return query.filter(func.lower(Invoice.InvoiceStatus).in_(_PENDING_STATUSES))
    if view == "approved":
        return query.filter(func.lower(Invoice.InvoiceStatus).in_(_APPROVED_STATUSES))
    if view == "rejected":
        return query.filter(func.lower(Invoice.InvoiceStatus).in_(_REJECTED_STATUSES))
    if view == "overdue":
        return query.filter(real_due, Invoice.DueDate < today, open_invoices)
    if view == "month":
        return query.filter(
            Invoice.InvoiceDate >= month_start.date(),
            Invoice.InvoiceDate >= _MIN_REAL_DATE,
        )
    if view == "week":
        return query.filter(
            Invoice.InvoiceDate >= week_start.date(),
            Invoice.InvoiceDate >= _MIN_REAL_DATE,
        )
    if view == "vendor" and vendor_id:
        return query.filter(Invoice.VendorID == vendor_id)
    if view == "approver" and approver:
        return (
            query.join(Approval, Approval.InvoiceID == Invoice.InvoiceID)
            .join(User, User.UserID == Approval.ApproverUserID)
            .filter(Approval.ApprovalStatus == "Pending", User.Username == approver)
            .distinct()
        )
    return query


def get_dashboard_metrics(view="all", vendor_id=None, approver=None):
    now = get_ist_now()
    view = (view or "all").strip().lower()
    if view not in _VIEWS:
        view = "all"
    if view == "vendor" and not vendor_id:
        view = "all"
    if view == "approver" and not approver:
        view = "all"

    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    week_start = now - timedelta(days=7)

    total_invoices = Invoice.query.count()

    status_rows = (
        Invoice.query.with_entities(
            Invoice.InvoiceStatus,
            func.count(Invoice.InvoiceID),
        )
        .group_by(Invoice.InvoiceStatus)
        .all()
    )
    global_status_counts = _status_counts_from_rows(status_rows)

    pending_approval = global_status_counts.get("Pending Approval", 0)
    approved = global_status_counts.get("Approved", 0)
    rejected = global_status_counts.get("Rejected", 0)

    total_value = (
        Invoice.query.with_entities(func.coalesce(func.sum(Invoice.InvoiceAmount), 0)).scalar()
    )
    month_filter = (
        Invoice.InvoiceDate >= month_start.date(),
        Invoice.InvoiceDate >= _MIN_REAL_DATE,
    )
    month_value = (
        Invoice.query.filter(*month_filter)
        .with_entities(func.coalesce(func.sum(Invoice.InvoiceAmount), 0))
        .scalar()
    )
    invoices_this_month = Invoice.query.filter(*month_filter).count()
    invoices_this_week = Invoice.query.filter(
        Invoice.InvoiceDate >= week_start.date(),
        Invoice.InvoiceDate >= _MIN_REAL_DATE,
    ).count()

    held_total = RejectedDocument.query.count()
    held_pending = RejectedDocument.query.filter_by(Decision="pending").count()

    today = now.date()
    week_end = today + timedelta(days=7)
    real_due = Invoice.DueDate >= _MIN_REAL_DATE
    open_invoices = _is_open_invoice()

    overdue_count = Invoice.query.filter(
        real_due, Invoice.DueDate < today, open_invoices,
    ).count()
    overdue_value = _decimal_to_float(
        Invoice.query.filter(real_due, Invoice.DueDate < today, open_invoices)
        .with_entities(func.coalesce(func.sum(Invoice.InvoiceAmount), 0))
        .scalar()
    )
    due_this_week = Invoice.query.filter(
        real_due,
        Invoice.DueDate >= today,
        Invoice.DueDate <= week_end,
        open_invoices,
    ).count()
    missing_due_date = Invoice.query.filter(Invoice.DueDate < _MIN_REAL_DATE).count()

    scoped_ids = None
    vendor_name = None
    if view == "held":
        scoped_ids = []
    elif view != "all":
        scoped_query = Invoice.query.with_entities(Invoice.InvoiceID)
        scoped_query = _apply_view(scoped_query, view, now, vendor_id, approver)
        scoped_ids = [row[0] for row in scoped_query.all()]
        if view == "vendor" and vendor_id:
            vendor = db.session.get(Vendor, vendor_id)
            vendor_name = vendor.VendorName if vendor else f"Vendor {vendor_id}"

    def _in_scope(query):
        if scoped_ids is None:
            return query
        if not scoped_ids:
            return query.filter(Invoice.InvoiceID == -1)
        return query.filter(Invoice.InvoiceID.in_(scoped_ids))

    held_rows = []
    if view == "held":
        held_docs = (
            RejectedDocument.query
            .order_by(RejectedDocument.CreatedDate.desc())
            .limit(50)
            .all()
        )
        held_rows = [
            {
                "rejection_id": row.RejectionID,
                "file_name": row.FileName or row.StoredFileName or "Document",
                "reason": row.Reason or "—",
                "vendor": row.VendorName or "—",
                "invoice_number": row.InvoiceNumber or "—",
                "decision": row.Decision or "pending",
                "amount": _decimal_to_float(row.InvoiceAmount),
                "currency": row.Currency or "",
            }
            for row in held_docs
        ]

    invoice_query = _in_scope(
        Invoice.query.options(joinedload(Invoice.vendor))
    ).order_by(Invoice.InvoiceDate.desc(), Invoice.InvoiceID.desc())
    invoice_rows = [_serialize_invoice(inv) for inv in invoice_query.limit(50).all()] if view != "held" else []

    scoped_status_rows = (
        _in_scope(Invoice.query.with_entities(
            Invoice.InvoiceStatus,
            func.count(Invoice.InvoiceID),
        ))
        .group_by(Invoice.InvoiceStatus)
        .all()
    )
    status_counts = _status_counts_from_rows(scoped_status_rows) if view != "held" else {}

    due_query = _in_scope(
        Invoice.query.options(joinedload(Invoice.vendor))
        .filter(real_due, Invoice.DueDate <= week_end, open_invoices)
    ).order_by(Invoice.DueDate.asc())
    if view == "overdue":
        due_query = _in_scope(
            Invoice.query.options(joinedload(Invoice.vendor))
            .filter(real_due, Invoice.DueDate < today, open_invoices)
        ).order_by(Invoice.DueDate.asc())
    due_dates = []
    if view != "held":
        for inv in due_query.limit(40).all():
            when, bucket = _due_bucket(inv, today)
            due_dates.append({
                "invoice_id": inv.InvoiceID,
                "invoice_number": inv.InvoiceNumber or f"INV-{inv.InvoiceID}",
                "vendor": inv.vendor.VendorName if inv.vendor else "—",
                "due_date": inv.DueDate.strftime("%d-%b-%Y"),
                "amount": _decimal_to_float(inv.InvoiceAmount),
                "currency": inv.Currency or "",
                "when": when,
                "bucket": bucket,
            })

    pending_rows = (
        Approval.query.options(
            joinedload(Approval.invoice).joinedload(Invoice.vendor),
            joinedload(Approval.user),
        )
        .filter_by(ApprovalStatus="Pending")
        .order_by(Approval.ApprovalDate.asc())
        .all()
    )
    if scoped_ids is not None:
        allowed = set(scoped_ids)
        pending_rows = [row for row in pending_rows if row.InvoiceID in allowed]

    aging = []
    for row in pending_rows[:40]:
        invoice = row.invoice
        aging.append({
            "invoice_id": invoice.InvoiceID if invoice else row.InvoiceID,
            "invoice_number": invoice.InvoiceNumber if invoice else str(row.InvoiceID),
            "vendor": (
                invoice.vendor.VendorName
                if invoice and invoice.vendor
                else "—"
            ),
            "approver": row.user.Username if row.user else str(row.ApproverUserID),
            "days": _days_open(row.ApprovalDate, now),
            "amount": _decimal_to_float(invoice.InvoiceAmount) if invoice else 0.0,
            "currency": invoice.Currency if invoice else "",
        })

    approver_queue_query = (
        db.session.query(User.Username, func.count(Approval.ApprovalID))
        .join(Approval, Approval.ApproverUserID == User.UserID)
        .filter(Approval.ApprovalStatus == "Pending")
    )
    if scoped_ids is not None:
        if scoped_ids:
            approver_queue_query = approver_queue_query.filter(Approval.InvoiceID.in_(scoped_ids))
        else:
            approver_queue_query = approver_queue_query.filter(Approval.InvoiceID == -1)
    approver_queue_rows = (
        approver_queue_query
        .group_by(User.UserID, User.Username)
        .order_by(func.count(Approval.ApprovalID).desc())
        .all()
    )
    approver_queue = [
        {"name": name, "pending": count}
        for name, count in approver_queue_rows
    ]

    vendor_query = (
        Invoice.query.join(Vendor)
        .with_entities(
            Vendor.VendorID,
            Vendor.VendorName,
            func.count(Invoice.InvoiceID),
            func.coalesce(func.sum(Invoice.InvoiceAmount), 0),
        )
    )
    vendor_query = _in_scope(vendor_query)
    top_vendor_rows = (
        vendor_query
        .group_by(Vendor.VendorID, Vendor.VendorName)
        .order_by(func.coalesce(func.sum(Invoice.InvoiceAmount), 0).desc())
        .limit(5)
        .all()
    )
    top_vendors = [
        {
            "vendor_id": vendor_pk,
            "name": name,
            "invoice_count": count,
            "total_value": _decimal_to_float(total),
        }
        for vendor_pk, name, count, total in top_vendor_rows
    ] if view != "held" else []

    if view == "held":
        filtered_count = held_total
        filtered_value = 0.0
        filter_label = "Held documents"
    elif scoped_ids is None:
        filtered_count = total_invoices
        filtered_value = _decimal_to_float(total_value)
        filter_label = "All invoices"
    else:
        filtered_count = len(scoped_ids)
        filtered_value = _decimal_to_float(
            Invoice.query.filter(Invoice.InvoiceID.in_(scoped_ids) if scoped_ids else Invoice.InvoiceID == -1)
            .with_entities(func.coalesce(func.sum(Invoice.InvoiceAmount), 0))
            .scalar()
        ) if scoped_ids else 0.0
        labels = {
            "pending": "Pending approval",
            "overdue": "Overdue",
            "month": "This month",
            "week": "This week",
            "approved": "Approved",
            "rejected": "Rejected",
            "vendor": vendor_name or "Vendor",
            "approver": f"Approver · {approver}",
        }
        filter_label = labels.get(view, "Filtered invoices")

    logger.debug(
        "[DASHBOARD] view=%s invoices=%s pending_approval=%s approved=%s rejected=%s held=%s",
        view, total_invoices, pending_approval, approved, rejected, held_total,
    )

    return {
        "generated_at": now.strftime("%d-%b-%Y %H:%M IST"),
        "view": view,
        "vendor_id": vendor_id,
        "approver": approver,
        "filter_label": filter_label,
        "filtered_count": filtered_count,
        "filtered_value": filtered_value,
        "kpis": {
            "total_invoices": total_invoices,
            "invoices_this_month": invoices_this_month,
            "invoices_this_week": invoices_this_week,
            "total_value": _decimal_to_float(total_value),
            "month_value": _decimal_to_float(month_value),
            "pending_approval": pending_approval,
            "approved": approved,
            "rejected": rejected,
            "held_documents": held_total,
            "held_pending": held_pending,
            "overdue": overdue_count,
            "overdue_value": overdue_value,
            "due_this_week": due_this_week,
            "missing_due_date": missing_due_date,
        },
        "status_counts": status_counts,
        "top_vendors": top_vendors,
        "aging": aging,
        "approver_queue": approver_queue,
        "due_dates": due_dates,
        "invoice_rows": invoice_rows,
        "held_rows": held_rows,
    }
