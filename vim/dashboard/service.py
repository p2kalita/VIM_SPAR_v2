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


def _decimal_to_float(value):
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def _count_status(status_counts, *names):
    wanted = {name.lower() for name in names}
    return sum(
        count
        for status, count in status_counts.items()
        if (status or "").lower() in wanted
    )


def _is_open_invoice():
    return ~func.lower(Invoice.InvoiceStatus).in_(_CLOSED_STATUSES)


def _days_open(start, now):
    if start is None:
        return 0
    if getattr(start, "tzinfo", None):
        start = start.replace(tzinfo=None)
    now_naive = now.replace(tzinfo=None) if getattr(now, "tzinfo", None) else now
    return max((now_naive - start).days, 0)


def get_dashboard_metrics():
    now = get_ist_now()
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
    status_counts = {status: count for status, count in status_rows}

    pending_approval = _count_status(status_counts, "Pending Approval")
    approved = _count_status(status_counts, "Approved")
    rejected = _count_status(status_counts, "Rejected")

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

    due_rows = (
        Invoice.query.options(joinedload(Invoice.vendor))
        .filter(real_due, Invoice.DueDate <= week_end, open_invoices)
        .order_by(Invoice.DueDate.asc())
        .limit(8)
        .all()
    )
    due_dates = []
    for inv in due_rows:
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
        due_dates.append({
            "invoice_number": inv.InvoiceNumber,
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
    aging = []
    for row in pending_rows[:8]:
        invoice = row.invoice
        aging.append({
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

    approver_queue_rows = (
        db.session.query(User.Username, func.count(Approval.ApprovalID))
        .join(Approval, Approval.ApproverUserID == User.UserID)
        .filter(Approval.ApprovalStatus == "Pending")
        .group_by(User.UserID, User.Username)
        .order_by(func.count(Approval.ApprovalID).desc())
        .all()
    )
    approver_queue = [
        {"name": name, "pending": count}
        for name, count in approver_queue_rows
    ]

    top_vendor_rows = (
        Invoice.query.join(Vendor)
        .with_entities(
            Vendor.VendorName,
            func.count(Invoice.InvoiceID),
            func.coalesce(func.sum(Invoice.InvoiceAmount), 0),
        )
        .group_by(Vendor.VendorID, Vendor.VendorName)
        .order_by(func.coalesce(func.sum(Invoice.InvoiceAmount), 0).desc())
        .limit(5)
        .all()
    )
    top_vendors = [
        {
            "name": name,
            "invoice_count": count,
            "total_value": _decimal_to_float(total),
        }
        for name, count, total in top_vendor_rows
    ]

    logger.debug(
        "[DASHBOARD] invoices=%s pending_approval=%s approved=%s rejected=%s held=%s",
        total_invoices, pending_approval, approved, rejected, held_total,
    )

    return {
        "generated_at": now.strftime("%d-%b-%Y %H:%M IST"),
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
    }
