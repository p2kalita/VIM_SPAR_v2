from vim_database.database import db
from vim_database.models import ValidationResult
from vim_logger import get_logger

logger = get_logger("vim.validation.result")


def save_validation_results(invoice, validation_result):
    """
    Store validation results for one invoice in validation_result table.
    """

    invoice_id = invoice.InvoiceID
    invoice_number = (
        validation_result.get("invoice_number")
        or getattr(invoice, "InvoiceNumber", None)
        or f"INV-{invoice_id}"
    )
    invoice_number = str(invoice_number).strip()[:100] or f"INV-{invoice_id}"

    results = validation_result.get(
        "validation_results",
        []
    )

    logger.info(
        "[RESULT] Saving %d validation result(s) for InvoiceID=%s ('%s')",
        len(results), invoice_id, invoice_number
    )

    deleted = ValidationResult.query.filter_by(
        InvoiceID=invoice_id
    ).delete()
    logger.debug("[RESULT] Cleared %d old result row(s) for InvoiceID=%s", deleted, invoice_id)

    for result in results:

        # --------------------------------------------------
        # Stage Status
        # --------------------------------------------------
        #
        # For now, this is derived from the existing
        # validation status.
        #
        # Later we can update this directly from each
        # stage when the stage starts/completes/fails.
        # --------------------------------------------------

        validation_status = result.get("status")

        if validation_status == "FAILED":
            stage_status = "failed"

        else:
            stage_status = "completed"

        validation_record = ValidationResult(
            InvoiceID=invoice_id,
            InvoiceNumber=invoice_number,
            ValidationType=result.get("stage"),

            # Existing logic - UNCHANGED
            ValidationStatus=validation_status,
            ValidationMessage=result.get("message"),
            ValidationDetails=result.get("details", {}),

            # NEW COLUMN ONLY
            StageStatus=stage_status
        )

        logger.debug(
            "[RESULT]   Stage='%s' Status='%s' StageStatus='%s'",
            result.get("stage"), validation_status, stage_status
        )
        db.session.add(validation_record)

    try:
        db.session.commit()
        logger.info(
            "[RESULT] Committed %d result row(s) for InvoiceID=%s",
            len(results), invoice_id
        )
    except Exception as e:
        db.session.rollback()
        logger.error(
            "[RESULT] Failed to commit validation results for InvoiceID=%s: %s",
            invoice_id, e, exc_info=True
        )
        raise