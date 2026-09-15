from .stages import (
    InvoiceCompletenessCheck,
    OcrConfidenceValidation,
    VendorValidation,
    POMatching,
    TaxValidation,
    DuplicateDetection,
    InvoiceAgeValidation
)
from vim_logger import get_logger

logger = get_logger("vim.validation.engine")


class ValidationEngine:

    def __init__(self):

        self.stages = [
            InvoiceCompletenessCheck(),
            OcrConfidenceValidation(),
            VendorValidation(),
            POMatching(),
            TaxValidation(),
            DuplicateDetection(),
            InvoiceAgeValidation()
        ]

    def validate_invoice(self, invoice, context=None):

        context = context or {}
        inv_num = invoice.get("invoice_number") or "<unknown>"
        logger.info("[ENGINE] Starting validation for invoice='%s'", inv_num)

        results = []

        for stage in self.stages:
            logger.info("[ENGINE]   Stage: %-30s ...", stage.name)
            result = stage.validate(
                invoice,
                context
            )
            status = result.get("status", "?")
            if status == "PASSED":
                logger.info("[ENGINE]   [PASS] %-30s -> %s", stage.name, status)
            elif status == "SKIPPED":
                logger.info("[ENGINE]   [SKIP] %-30s -> %s", stage.name, status)
            else:
                logger.warning(
                    "[ENGINE]   [FAIL] %-30s -> %s | %s",
                    stage.name, status, result.get("message", "")
                )
            results.append(result)

        passed = sum(
            1 for result in results
            if result["status"] == "PASSED"
        )

        failed = sum(
            1 for result in results
            if result["status"] == "FAILED"
        )

        overall_status = (
            "PASSED"
            if failed == 0
            else "FAILED"
        )

        logger.info(
            "[ENGINE] Summary for '%s': %s  (%d passed, %d failed out of %d stages)",
            inv_num, overall_status, passed, failed, len(results)
        )

        return {
            "invoice_number": invoice.get("invoice_number"),
            "overall_status": overall_status,
            "total_stages": len(results),
            "stages_passed": passed,
            "stages_failed": failed,
            "validation_results": results
        }

    def validate_invoices(self, invoices, context=None):

        reports = []

        for invoice in invoices:

            report = self.validate_invoice(
                invoice,
                context
            )

            reports.append(report)

        return reports