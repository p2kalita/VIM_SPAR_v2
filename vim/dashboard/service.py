from datetime import datetime, timedelta
from decimal import Decimal
from vim.timezone import get_ist_now

from sqlalchemy import func

from vim_database.models import (
    Invoice,
    OCRExtraction,
    User,
    ValidationResult,
    Vendor,
)

_FAILED_STATUSES = ["FAILED", "Failed", "FAIL"]


def _decimal_to_float(value):
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def _distinct_invoice_count(query_filter):
    return (
        query_filter.with_entities(func.count(func.distinct(ValidationResult.InvoiceID)))
        .scalar()
        or 0
    )


def get_dashboard_metrics():
    now = get_ist_now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    week_start = now - timedelta(days=7)

    total_invoices = Invoice.query.count()
    total_vendors = Vendor.query.count()
    active_vendors = Vendor.query.filter_by(Status=1).count()
    total_users = User.query.count()
    active_users = User.query.filter_by(IsActive=True).count()

    status_rows = (
        Invoice.query.with_entities(
            Invoice.InvoiceStatus,
            func.count(Invoice.InvoiceID),
        )
        .group_by(Invoice.InvoiceStatus)
        .all()
    )
    status_counts = {status: count for status, count in status_rows}

    total_value = (
        Invoice.query.with_entities(func.coalesce(func.sum(Invoice.InvoiceAmount), 0)).scalar()
    )
    month_value = (
        Invoice.query.filter(Invoice.InvoiceDate >= month_start.date())
        .with_entities(func.coalesce(func.sum(Invoice.InvoiceAmount), 0))
        .scalar()
    )
    invoices_this_month = Invoice.query.filter(
        Invoice.InvoiceDate >= month_start.date()
    ).count()
    invoices_this_week = Invoice.query.filter(
        Invoice.InvoiceDate >= week_start.date()
    ).count()

    failed_filter = ValidationResult.ValidationStatus.in_(_FAILED_STATUSES)
    invoices_failed_validation = _distinct_invoice_count(
        ValidationResult.query.filter(failed_filter)
    )

    failed_ids = [
        row[0]
        for row in ValidationResult.query.filter(failed_filter)
        .with_entities(ValidationResult.InvoiceID)
        .distinct()
        .all()
    ]
    if failed_ids:
        invoices_passed_validation = _distinct_invoice_count(
            ValidationResult.query.filter(~ValidationResult.InvoiceID.in_(failed_ids))
        )
    else:
        invoices_passed_validation = _distinct_invoice_count(ValidationResult.query)

    ocr_extractions = OCRExtraction.query.count()
    avg_confidence = (
        OCRExtraction.query.with_entities(
            func.coalesce(func.avg(OCRExtraction.ConfidenceScore), 0)
        ).scalar()
    )
    ocr_status_rows = (
        OCRExtraction.query.with_entities(
            OCRExtraction.ExtractionStatus,
            func.count(OCRExtraction.ExtractionID),
        )
        .group_by(OCRExtraction.ExtractionStatus)
        .all()
    )
    ocr_status_counts = {status: count for status, count in ocr_status_rows}

    validation_failure_rows = (
        ValidationResult.query.filter(failed_filter)
        .with_entities(
            ValidationResult.ValidationType,
            func.count(ValidationResult.ValidationID),
        )
        .group_by(ValidationResult.ValidationType)
        .order_by(func.count(ValidationResult.ValidationID).desc())
        .all()
    )
    validation_issue_counts = {
        validation_type: count for validation_type, count in validation_failure_rows
    }

    top_vendor_rows = (
        Invoice.query.join(Vendor)
        .with_entities(
            Vendor.VendorName,
            func.count(Invoice.InvoiceID),
            func.coalesce(func.sum(Invoice.InvoiceAmount), 0),
        )
        .group_by(Vendor.VendorID, Vendor.VendorName)
        .order_by(func.count(Invoice.InvoiceID).desc())
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

    recent_invoices = (
        Invoice.query.order_by(Invoice.InvoiceID.desc()).limit(5).all()
    )

    cost_over_time_rows = (
        Invoice.query.with_entities(
            Invoice.InvoiceDate,
            func.coalesce(func.sum(Invoice.InvoiceAmount), 0),
        )
        .group_by(Invoice.InvoiceDate)
        .order_by(Invoice.InvoiceDate)
        .all()
    )
    cost_over_time = {
        "labels": [row[0].strftime("%d-%b-%Y") for row in cost_over_time_rows],
        "amounts": [_decimal_to_float(row[1]) for row in cost_over_time_rows],
    }

    return {
        "generated_at": now.strftime("%d-%b-%Y %H:%M IST"),
        "kpis": {
            "total_invoices": total_invoices,
            "invoices_failed_validation": invoices_failed_validation,
            "invoices_passed_validation": invoices_passed_validation,
            "total_value": _decimal_to_float(total_value),
            "month_value": _decimal_to_float(month_value),
            "invoices_this_month": invoices_this_month,
            "invoices_this_week": invoices_this_week,
            "total_vendors": total_vendors,
            "active_vendors": active_vendors,
            "total_users": total_users,
            "active_users": active_users,
            "ocr_extractions": ocr_extractions,
            "avg_extraction_confidence": round(_decimal_to_float(avg_confidence), 1),
        },
        "status_counts": status_counts,
        "ocr_status_counts": ocr_status_counts,
        "validation_issue_counts": validation_issue_counts,
        "top_vendors": top_vendors,
        "recent_invoices": recent_invoices,
        "cost_over_time": cost_over_time,
    }
