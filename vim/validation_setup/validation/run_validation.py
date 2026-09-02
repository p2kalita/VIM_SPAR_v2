import json
from pathlib import Path

from vim.validation_setup.validation.validation_engine import ValidationEngine
from vim_database.models import Invoice
from vim.extraction.json_store import ENRICHED_PATH
from vim_logger import get_logger

from vim.validation_setup.validation.validation_result import (
    save_validation_results
)

logger = get_logger("vim.validation.runner")


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
        invoice_id_set = set(invoice_ids)
        invoices = [
            inv for inv in all_invoices
            if inv.get("invoice_id") in invoice_id_set
        ]
        logger.info(
            "[VALIDATION] Filtered to %d/%d invoice(s) matching uploaded IDs: %s",
            len(invoices), len(all_invoices), invoice_ids
        )
        if not invoices:
            logger.warning(
                "[VALIDATION] No enriched.json records matched the uploaded invoice IDs %s "
                "— falling back to validating all records",
                invoice_ids
            )
            invoices = all_invoices
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

        # --------------------------------------------------
        # Get business invoice number
        # --------------------------------------------------

        invoice_number = result.get("invoice_number")

        # --------------------------------------------------
        # Find actual invoice record in database
        # --------------------------------------------------

        invoice = Invoice.query.filter_by(
            InvoiceNumber=invoice_number
        ).first()

        if not invoice:
            logger.warning(
                "[VALIDATION] Invoice '%s' NOT FOUND in database — skipping DB save",
                invoice_number
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