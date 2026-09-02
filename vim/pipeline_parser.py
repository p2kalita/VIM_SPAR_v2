"""
vim/pipeline_parser.py
─────────────────────────────────────────────────────────────────
Parses vim.log to extract per-invoice pipeline data for the
Pipeline Monitor page.  No technical jargon is exposed to callers.
─────────────────────────────────────────────────────────────────
"""

import re
from pathlib import Path
from datetime import datetime
from vim_logger import get_logger

logger = get_logger("vim.pipeline_parser")

_LOG_FILE = Path(__file__).resolve().parent.parent / "logs" / "vim.log"

# ── Regex patterns ────────────────────────────────────────────────────────────
_TS_PATTERN       = r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
_PIPELINE_START   = re.compile(r"\[PIPELINE START\] File: '(.+?)'")
_PIPELINE_END     = re.compile(r"\[PIPELINE END\] '(.+?)' → status=(\w+)")
_UPLOAD_DONE      = re.compile(r"\[UPLOAD\] Saved '(.+?)'")
_VENDOR_DETECTED  = re.compile(r"\[VENDOR DETECT\] Identified vendor: '(.+?)'")
_VENDOR_MATCHED   = re.compile(r"\[STEP 3/6\] Vendor matched: '(.+?)'")
_EXTRACT_OK       = re.compile(r"\[STEP 4/6\] Extraction OK — invoice_number='(.+?)', total_due=([\d.]+)")
_DB_PERSIST_OK    = re.compile(r"\[STEP 6/6\] DB persist OK — InvoiceID=(\d+), InvoiceNumber='(.+?)'")
_ENGINE_STAGE     = re.compile(r"\[ENGINE\]\s+Stage: (.+?)\s+\.\.\.")
_ENGINE_RESULT    = re.compile(r"\[ENGINE\]\s+[✔✘]\s+(.+?)\s+→ (PASSED|FAILED)(?:\s+\|\s+(.+))?")
_ENGINE_SUMMARY   = re.compile(r"\[ENGINE\] Summary for '(.+?)': (PASSED|FAILED)\s+\((\d+) passed, (\d+) failed")
_TIMESTAMP        = re.compile(_TS_PATTERN)

# ── Friendly labels (for non-technical users) ─────────────────────────────────
_STAGE_LABELS = {
    "Invoice Completeness": ("📋", "All required invoice fields are present"),
    "OCR Confidence":       ("🔍", "Document text was read clearly"),
    "Vendor Validation":    ("🏢", "Vendor identity confirmed"),
    "PO Matching":          ("📦", "Linked to a purchase order"),
    "Tax Validation":       ("💰", "Tax figures are correct"),
    "Duplicate Detection":  ("🔄", "Checked for duplicate submissions"),
}

_FAIL_REASONS = {
    "PO number is missing":                             "No purchase order number was found on this invoice. Please ensure a PO number is included.",
    "Duplicate invoice found within the last 30 days":  "This invoice appears to have already been submitted recently. Please check for duplicate submissions.",
}


def _friendly_fail(raw: str) -> str:
    for key, msg in _FAIL_REASONS.items():
        if key.lower() in raw.lower():
            return msg
    return raw.strip() if raw else "An issue was detected. Please review this invoice."


def parse_log() -> list[dict]:
    """
    Read vim.log and return a list of pipeline records, one per invoice run,
    most-recent first.  Each record contains only client-friendly fields.
    """
    if not _LOG_FILE.exists():
        return []

    text = _LOG_FILE.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    invoices: list[dict] = []
    current: dict | None = None

    for line in lines:
        # ── Start of a new pipeline run ───────────────────────────────────────
        m = _PIPELINE_START.search(line)
        if m:
            # push previous if incomplete
            if current:
                invoices.append(current)
            ts_m = _TIMESTAMP.search(line)
            current = {
                "filename":       m.group(1),
                "started_at":     ts_m.group(1) if ts_m else "",
                "finished_at":    "",
                "invoice_number": "",
                "vendor":         "",
                "amount":         "",
                # Pipeline stages (plain labels)
                "stage_upload":      "in_progress",
                "stage_extract":     "waiting",
                "stage_validate":    "waiting",
                # Sub-checks within validation
                "checks":            [],
                # Overall
                "overall":          "in_progress",
                "overall_label":    "Processing…",
                "current_step_msg": "Invoice received — reading document…",
                "issues":           [],
            }
            continue

        if current is None:
            continue

        ts_m = _TIMESTAMP.search(line)
        ts = ts_m.group(1) if ts_m else current.get("started_at", "")

        # ── Upload done ───────────────────────────────────────────────────────
        if _UPLOAD_DONE.search(line):
            current["stage_upload"] = "done"
            current["current_step_msg"] = "Document uploaded successfully — identifying vendor…"

        # ── Vendor detected ───────────────────────────────────────────────────
        m = _VENDOR_DETECTED.search(line)
        if m:
            current["vendor"] = m.group(1).title()
            current["current_step_msg"] = f"Vendor identified as {m.group(1).title()} — reading invoice details…"

        # ── Extraction OK ─────────────────────────────────────────────────────
        m = _EXTRACT_OK.search(line)
        if m:
            current["invoice_number"] = m.group(1)
            current["amount"]         = m.group(2)
            current["stage_extract"]  = "done"
            current["current_step_msg"] = "Invoice data extracted — running checks…"
            current["stage_validate"] = "in_progress"

        # ── DB persist OK ─────────────────────────────────────────────────────
        m = _DB_PERSIST_OK.search(line)
        if m:
            current["invoice_number"] = m.group(2)

        # ── Validation check result ───────────────────────────────────────────
        m = _ENGINE_RESULT.search(line)
        if m:
            check_name = m.group(1).strip()
            status     = m.group(2)        # PASSED | FAILED
            raw_reason = m.group(3) or ""
            icon, desc = _STAGE_LABELS.get(check_name, ("✅", check_name))
            current["checks"].append({
                "name":    check_name,
                "label":   desc,
                "icon":    icon,
                "status":  status,
                "reason":  _friendly_fail(raw_reason) if status == "FAILED" else "",
            })
            if status == "FAILED":
                current["issues"].append(_friendly_fail(raw_reason))

        # ── Validation summary ────────────────────────────────────────────────
        m = _ENGINE_SUMMARY.search(line)
        if m:
            inv_no   = m.group(1)
            outcome  = m.group(2)
            passed   = int(m.group(3))
            failed   = int(m.group(4))
            current["invoice_number"] = current["invoice_number"] or inv_no
            if outcome == "PASSED":
                current["stage_validate"]    = "done"
                current["overall"]           = "passed"
                current["overall_label"]     = "All Checks Passed"
                current["current_step_msg"]  = f"All {passed} checks passed — invoice is ready for approval."
            else:
                current["stage_validate"]    = "issues"
                current["overall"]           = "issues"
                current["overall_label"]     = f"{failed} Issue{'s' if failed > 1 else ''} Found"
                current["current_step_msg"]  = f"{failed} check{'s' if failed > 1 else ''} need{'s' if failed == 1 else ''} attention — see details below."

        # ── Pipeline end ──────────────────────────────────────────────────────
        m = _PIPELINE_END.search(line)
        if m:
            status = m.group(2)
            current["finished_at"] = ts
            if status == "success" and current["stage_upload"] == "done":
                pass   # overall already set by validation summary
            elif status != "success":
                current["overall"]       = "error"
                current["overall_label"] = "Processing Failed"
                current["current_step_msg"] = "Something went wrong while processing this invoice. Please contact support."
            invoices.append(current)
            current = None

    # Append any still-open run (in-flight)
    if current:
        invoices.append(current)

    # Most recent first
    invoices.reverse()
    return invoices
