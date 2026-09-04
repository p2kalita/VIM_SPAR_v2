import json

from vim.validation_setup.validation.validation_engine import ValidationEngine
from vim_database.database import db
from vim_database.models import Invoice
from vim.extraction.json_store import ENRICHED_PATH
from vim_logger import get_logger

from vim.validation_setup.validation.validation_result import (
    save_validation_results
)

logger = get_logger("vim.validation.runner")


def _as_int(value):
    """Coerce JSON / session values to int. JSON may store InvoiceID as a string."""
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _resolve_invoice(invoice_data: dict, result: dict):
    """Match the engine result to the DB row that the upload just created.

    Lookup by InvoiceNumber is wrong for bulk: several files can share a
    number (or have none), so `.first()` attaches results to the wrong row
    or skips the save. InvoiceID from enriched.json is the stable key.
    """
    iid = _as_int(invoice_data.get("invoice_id"))
    if iid is not None:
        invoice = db.session.get(Invoice, iid)
        if invoice:
            return invoice

    invoice_number = (
        result.get("invoice_number")
        or invoice_data.get("invoice_number")
    )
    if invoice_number:
        invoice = Invoice.query.filter_by(
            InvoiceNumber=str(invoice_number).strip()
        ).first()
        if invoice:
            return invoice

    return None


def _invoice_to_validation_record(invoice: Invoice) -> dict:
    """Build an engine payload from the DB when enriched.json has no row."""
    vendor = invoice.vendor
    items = []
    for item in invoice.line_items or []:
        items.append({
            "description": item.Description,
            "item_type": item.ItemType,
            "quantity": float(item.Quantity) if item.Quantity is not None else None,
            "unit_of_measure": item.UnitOfMeasure,
            "unit_price": float(item.CostAmount) if item.CostAmount is not None else None,
            "tax_rate": float(item.TaxRate) if item.TaxRate is not None else None,
            "tax_amount": float(item.TaxAmount) if item.TaxAmount is not None else None,
            "amount": float(item.LineAmount) if item.LineAmount is not None else None,
            "po_number": item.PONumber,
        })
    return {
        "invoice_id": invoice.InvoiceID,
        "invoice_number": invoice.InvoiceNumber,
        "invoice_date": str(invoice.InvoiceDate) if invoice.InvoiceDate else None,
        "vendor_id": invoice.VendorID,
        "vendor_name": vendor.VendorName if vendor else None,
        "line_items": items,
        "total_due": float(invoice.InvoiceAmount) if invoice.InvoiceAmount is not None else None,
        "currency": invoice.Currency,
        "due_date": str(invoice.DueDate) if invoice.DueDate else None,
        "po_number": invoice.PONumber,
        "tax_amount": float(invoice.TaxAmount) if invoice.TaxAmount is not None else None,
        "tax_rate": float(invoice.TaxRate) if invoice.TaxRate is not None else None,
        "net_amount": float(invoice.NetAmount) if invoice.NetAmount is not None else None,
        "gross_amount": float(invoice.GrossAmount) if invoice.GrossAmount is not None else None,
    }


def _records_for_ids(all_invoices: list, invoice_ids: list[int]) -> list:
    invoice_id_set = {i for i in (_as_int(x) for x in invoice_ids) if i is not None}
    invoices = [
        inv for inv in all_invoices
        if _as_int(inv.get("invoice_id")) in invoice_id_set
    ]
    found = {_as_int(inv.get("invoice_id")) for inv in invoices}
    missing = [iid for iid in invoice_id_set if iid not in found]
    if missing:
        logger.warning(
            "[VALIDATION] %d uploaded ID(s) missing from enriched.json — loading from DB: %s",
            len(missing), missing,
        )
        for iid in missing:
            row = db.session.get(Invoice, iid)
            if row:
                invoices.append(_invoice_to_validation_record(row))
            else:
                logger.warning("[VALIDATION] InvoiceID=%s not in database either", iid)
    logger.info(
        "[VALIDATION] Filtered to %d invoice(s) matching uploaded IDs: %s",
        len(invoices), invoice_ids,
    )
    return invoices


def run_validation(invoice_ids: list[int] | None = None):
    """
    Run business validation on enriched invoices.

    Args:
        invoice_ids: Optional list of InvoiceIDs to restrict validation to.
                     When None, ALL records in enriched.json are validated.
                     Pass the list of IDs returned from the upload pipeline to
                     validate only the newly uploaded invoices.
    """

    # --------------------------------------------------
    # Path to enriched.json  (resolved dynamically from config)
    # --------------------------------------------------

    enriched_file = ENRICHED_PATH

    # --------------------------------------------------
    # Read enriched.json
    # --------------------------------------------------

    if not enriched_file.exists():
        logger.warning("[VALIDATION] enriched.json not found at %s — skipping", enriched_file)
        return False

    logger.info("[VALIDATION] Reading enriched.json from: %s", enriched_file)
    with open(enriched_file, "r", encoding="utf-8") as file:
        all_invoices = json.load(file)

    # --------------------------------------------------
    # Filter to the invoices that were just uploaded
    # --------------------------------------------------

    if invoice_ids is not None:
        invoices = _records_for_ids(all_invoices, invoice_ids)
        if not invoices:
            logger.warning(
                "[VALIDATION] No records found for uploaded invoice IDs %s",
                invoice_ids,
            )
            return False
    else:
        invoices = all_invoices
        logger.info("[VALIDATION] Found %d invoice record(s) to validate", len(invoices))

    # --------------------------------------------------
    # Create validation engine
    # --------------------------------------------------

    engine = ValidationEngine()

    saved_count = 0

    # --------------------------------------------------
    # Process every invoice
    # --------------------------------------------------

    for idx, invoice_data in enumerate(invoices):

        inv_num = invoice_data.get("invoice_number") or f"<unknown #{idx}>"
        logger.info("-" * 50)
        logger.info(
            "[VALIDATION] Processing invoice %d/%d — number='%s', vendor='%s'",
            idx + 1, len(invoices), inv_num, invoice_data.get("vendor_name")
        )

        # Run validation stages
        try:
            result = engine.validate_invoice(
                invoice_data,
                context={}
            )
        except Exception as e:
            logger.error(
                "[VALIDATION] Engine crashed on invoice '%s': %s", inv_num, e, exc_info=True
            )
            continue

        logger.info(
            "[VALIDATION] Result for '%s': overall=%s  passed=%d  failed=%d",
            inv_num,
            result.get("overall_status"),
            result.get("stages_passed"),
            result.get("stages_failed")
        )

        invoice = _resolve_invoice(invoice_data, result)

        if not invoice:
            logger.warning(
                "[VALIDATION] Invoice not found in database — skipping DB save "
                "(invoice_id=%s, invoice_number='%s')",
                invoice_data.get("invoice_id"),
                result.get("invoice_number") or invoice_data.get("invoice_number"),
            )
            continue

        logger.debug("[VALIDATION] Matched DB invoice: InvoiceID=%s", invoice.InvoiceID)

        # --------------------------------------------------
        # Store validation results in DB
        # --------------------------------------------------

        try:
            save_validation_results(invoice, result)
            saved_count += 1
            logger.info("[VALIDATION] Results saved to DB for InvoiceID=%s", invoice.InvoiceID)
        except Exception as e:
            logger.error(
                "[VALIDATION] Failed to save results for InvoiceID=%s: %s",
                invoice.InvoiceID, e, exc_info=True
            )

    logger.info("-" * 50)
    logger.info(
        "[VALIDATION] Run complete — %d/%d invoice(s) saved to DB",
        saved_count, len(invoices)
    )
    return saved_count > 0