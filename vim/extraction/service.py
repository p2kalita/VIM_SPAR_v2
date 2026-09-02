"""Orchestrates upload → extract → persist for the VIM web app."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

from werkzeug.utils import secure_filename

from vim.extraction import config
from vim.extraction.classifier import (
    classify_document,
    classify_image,
    purchase_order_hold_reason,
)
from vim.extraction.enrich import extract_from_file, load_document_text
from vim.extraction.load import insert_record
from vim.extraction.schema import empty_record
from vim.extraction.vendors import find_or_create_vendor
from vim_database.database import db
from vim_logger import get_logger

logger = get_logger("vim.extraction.service")


# SQLite permits a single writer, so parallel uploads take turns for the
# persist step rather than racing and failing with "database is locked".
_db_write_lock = threading.Lock()
 
def allowed_file(filename: str) -> bool:
    return Path(filename).suffix.lower() in config.SUPPORTED_EXTENSIONS
 
 
def save_upload(file_storage) -> Path:
    """Save an uploaded file and return its path."""
    original = secure_filename(file_storage.filename or "")
    if not original:
        raise ValueError("No filename provided")
 
    ext = Path(original).suffix.lower()
    if ext not in config.SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported file type: {ext}")
 
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.UPLOAD_DIR / f"{uuid4().hex}_{original}"
    file_storage.save(dest)
    logger.debug("[SAVE] Stored '%s' -> %s", original, dest.name)
    return dest
 
 
def resolve_pending_upload(stored_file_name: str) -> Path | None:
    """
    Map a stored upload name back to its path on disk.
 
    Only names that resolve to an existing file directly inside UPLOAD_DIR are
    accepted, so a crafted value cannot reach elsewhere on the filesystem.
    """
    name = Path(stored_file_name or "").name
    if not name:
        return None
 
    upload_dir = config.UPLOAD_DIR.resolve()
    candidate = (upload_dir / name).resolve()
    if candidate.parent != upload_dir or not candidate.is_file():
        return None
    return candidate
 
 
def discard_pending_upload(
    stored_file_name: str,
    original_name: str = "",
    user_id=None,
) -> bool:
    """
    Decline a held upload: keep the file and a rejected_document row for
    later review, but drop it from enriched.json so it is not treated as live.
    """
    from vim.extraction.json_store import delete_record
    from vim.extraction.rejections import DECISION_STOPPED, mark_decision
 
    logger.info("[DISCARD] user_id=%s discarding upload '%s'", user_id, stored_file_name)
    mark_decision(stored_file_name, DECISION_STOPPED, user_id=user_id)
    delete_record(
        original_name or stored_file_name,
        stored_file_name=stored_file_name,
    )
    logger.debug("[DISCARD] enriched.json record removed for '%s'", stored_file_name)
    return True
 
 
def _hold_as_rejected(record: dict, reason: str) -> dict:
    """Write enriched.json and a rejected_document row, then return the record."""
    from vim.extraction.json_store import upsert_record
    from vim.extraction.rejections import record_rejection
 
    logger.info("[REJECT] '%s' held as '%s'", record.get("file_name"), reason)
    upsert_record(record)
    try:
        record_rejection(record, reason=reason)
    except Exception as e:
        logger.error("[REJECT] Could not write rejected_document row for '%s': %s",
                     record.get("file_name"), e, exc_info=True)
    return record
 
 
def _resolve_status(record: dict) -> str:
    """Derive pipeline status from extraction/validation outcome."""
    if record.get("invoice_id") is not None:
        if record.get("_validation_issues"):
            return "needs_review"
        return "success"
    if record.get("status") == "vendor_not_registered":
        return "vendor_not_registered"
    if record.get("status") == "not_invoice":
        return "not_invoice"
    if record.get("status") == "incomplete_header":
        return "incomplete_header"
    if record.get("_extraction_error"):
        return "extraction_failed"
    if record.get("_db_error") or record.get("status") == "db_error":
        return "db_error"
    if record.get("_validation_issues"):
        return "needs_review"
    return "success"


def _hold_incomplete_header(
    record: dict,
    original_name: str,
    saved_path: Path,
) -> dict:
    """
    Keep the extracted snapshot but do not create an invoice.

    Without a vendor name there is no VendorID, so the invoice and
    validation_result tables cannot be written. The trace lives on
    rejected_document instead.
    """
    from vim.extraction.rejections import REASON_INCOMPLETE_HEADER

    missing = ["vendor_name"]
    if not str(record.get("invoice_number") or "").strip():
        missing.append("invoice_number")

    record["file_name"] = original_name
    record["stored_file_name"] = saved_path.name if saved_path else record.get("stored_file_name")
    record["file_path"] = str(saved_path) if saved_path else record.get("file_path")
    record["vendor_id"] = None
    record["status"] = "incomplete_header"
    record["_missing_fields"] = missing
    record["_incomplete_reason"] = (
        "Could not read the issuing vendor from this document. "
        "No invoice was saved because a vendor name is required to "
        "create a vendor and invoice row. Review it under Rejected Documents."
    )
    record.pop("_extraction_error", None)
    logger.warning(
        "[PERSIST] Incomplete header on '%s' — missing %s; held as rejected_document",
        original_name,
        missing,
    )
    return _hold_as_rejected(record, REASON_INCOMPLETE_HEADER)
 
 
def _not_invoice_result(
    *,
    original_name: str,
    saved_path: Path,
    raw_text: str,
    verdict: dict,
) -> dict:
    """Awaiting-decision result for a document the classifier rejected."""
    record = empty_record()
    record["file_name"] = original_name
    record["stored_file_name"] = saved_path.name
    record["file_path"] = str(saved_path)
    record["raw_text"] = raw_text
    record["status"] = "not_invoice"
    record["_classification"] = verdict
    record["_not_invoice_reason"] = (
        verdict.get("reason") or "Gemini did not recognise this as an invoice."
    )
    record["_document_type"] = verdict.get("document_type")
    return record
 
 
def stage_upload(file_storage) -> tuple[Path, str]:
    """
    Save an upload to disk without processing it.

    Splitting this out lets the web request finish as soon as the bytes are
    stored, and hand the slow extraction to a background job.
    """
    config.validate()

    original_name = secure_filename(file_storage.filename or "")
    logger.info("[STAGE] Staging upload: '%s'", original_name)
    saved_path = save_upload(file_storage)
    return saved_path, original_name
 
 
def process_uploaded_file(file_storage) -> dict:
    """
    Full intelligent upload pipeline:
    1. Save file
    2. Confirm the document is an invoice (Gemini gate), in parallel with
       extracting it (LlamaParse + Groq)
    3. Validate
    4. Resolve the vendor, registering it when it is new
    5. Save to output/enriched.json
    6. Persist to VIM database (skipped when extraction fails)
 
    A document the classifier rejects comes back with status "not_invoice"
    and is left on disk so the user can choose to proceed or discard it.
    """
    saved_path, original_name = stage_upload(file_storage)
    return process_saved_file(saved_path, original_name)
 
 
def process_saved_file(
    saved_path: Path,
    original_name: str,
    *,
    skip_invoice_check: bool = False,
) -> dict:
    """
    Run the pipeline on a file already saved to the upload directory.
 
    Set skip_invoice_check to resume a document the classifier rejected,
    after the user chose to proceed anyway.
    """
    from vim.extraction.json_store import upsert_record
 
    config.validate()
    started = time.perf_counter()

    def _log(step: str) -> None:
        elapsed = time.perf_counter() - started
        logger.info("[PIPELINE] '%s' | %s (%.1fs)", original_name, step, elapsed)

    is_image = saved_path.suffix.lower() in config.IMAGE_EXTENSIONS
 
    # Images are judged from the pixels and extracted by vision, so OCR is
    # skipped entirely. LlamaParse on a PNG is the slowest path we have.
    if is_image:
        if not skip_invoice_check:
            _log("classifying image")
            verdict = classify_image(saved_path, original_name)
            if verdict.get("error"):
                logger.warning("[CLASSIFY] image classification unavailable for '%s': %s",
                               original_name, verdict["error"])
            elif verdict.get("is_invoice") is False:
                record = _not_invoice_result(
                    original_name=original_name,
                    saved_path=saved_path,
                    raw_text="",
                    verdict=verdict,
                )
                return _hold_as_rejected(record, "not_invoice")
 
        _log("extracting image")
        record = extract_from_file(str(saved_path), raw_text=None)
        record["file_name"] = original_name
        record["stored_file_name"] = saved_path.name
        if record.get("_extraction_error"):
            record["status"] = "extraction_failed"
            upsert_record(record)
            return record
        if not skip_invoice_check:
            held = _hold_if_purchase_order(record, original_name, saved_path)
            if held is not None:
                return held
        return _persist_record(record, original_name, saved_path)
 
    _log("parsing text")
    raw_text, parse_error = load_document_text(str(saved_path))
    if parse_error:
        logger.error("[PIPELINE] text parse failed for '%s': %s", original_name, parse_error)
        record = empty_record()
        record["file_name"] = original_name
        record["stored_file_name"] = saved_path.name
        record["file_path"] = str(saved_path)
        record["_extraction_error"] = parse_error
        record["status"] = "extraction_failed"
        upsert_record(record)
        return record
 
    # Classification and extraction both need only raw_text, so they run
    # concurrently instead of one after the other. The cost is an extraction
    # call wasted on the rare document that turns out not to be an invoice;
    # the saving is the classifier's latency on every document that is one.
    if not skip_invoice_check:
        _log("classifying + extracting in parallel")
        with ThreadPoolExecutor(max_workers=2) as pool:
            verdict_task = pool.submit(classify_document, raw_text, original_name)
            record_task = pool.submit(
                extract_from_file, str(saved_path), raw_text=raw_text
            )
            verdict = verdict_task.result()
            record = record_task.result()
 
        # A classifier outage must not block the pipeline; note it and continue.
        if verdict.get("error"):
            logger.warning("[CLASSIFY] text classification unavailable for '%s': %s",
                           original_name, verdict["error"])
        elif verdict.get("is_invoice") is False:
            record["raw_text"] = raw_text
            return _hold_not_invoice_keep_extraction(
                record, original_name, saved_path, verdict
            )
    else:
        _log("extracting")
        record = extract_from_file(str(saved_path), raw_text=raw_text)

    record["file_name"] = original_name
    record["stored_file_name"] = saved_path.name
    if not record.get("raw_text"):
        record["raw_text"] = raw_text

    if not skip_invoice_check:
        held = _hold_if_purchase_order(record, original_name, saved_path)
        if held is not None:
            return held

    _log("persisting")
    record = _persist_record(record, original_name, saved_path)
    _log(f"done status={record.get('status')}")
    return record
 
 
def _hold_if_purchase_order(
    record: dict,
    original_name: str,
    saved_path: Path,
    *,
    vendor_approved: bool = False,
):
    """Hold a PO for proceed/stop instead of saving it as an invoice."""
    reason = purchase_order_hold_reason(record)
    if not reason:
        return None

    logger.info("[PIPELINE] '%s' held as purchase order: %s", original_name, reason)
    record["document_type"] = record.get("document_type") or "Purchase Order"
    if record.get("document_type_code") == 0:
        record["document_type_code"] = 3
    verdict = {
        "is_invoice": False,
        "document_type": "Purchase Order",
        "confidence": 95,
        "reason": reason,
    }
    return _hold_not_invoice_keep_extraction(
        record, original_name, saved_path, verdict, vendor_approved=vendor_approved
    )


def _classify_held_record(saved_path: Path, original_name: str, record: dict) -> dict:
    """Run the invoice classifier on a file already extracted and sitting on disk."""
    is_image = saved_path.suffix.lower() in config.IMAGE_EXTENSIONS
    if is_image:
        return classify_image(saved_path, original_name)

    raw_text = (record.get("raw_text") or "").strip()
    if not raw_text:
        raw_text, parse_error = load_document_text(str(saved_path))
        if parse_error:
            logger.warning(
                "[VENDOR-APPROVE] Could not re-read text for '%s': %s",
                original_name, parse_error,
            )
            return {"is_invoice": None, "error": parse_error}
        record["raw_text"] = raw_text
    return classify_document(raw_text, original_name)


def _hold_not_invoice_keep_extraction(
    record: dict,
    original_name: str,
    saved_path: Path,
    verdict: dict,
    *,
    vendor_approved: bool = False,
) -> dict:
    """Hold as not-an-invoice without wiping the extracted vendor snapshot."""
    record["file_name"] = original_name
    record["stored_file_name"] = saved_path.name if saved_path else record.get("stored_file_name")
    record["file_path"] = str(saved_path) if saved_path else record.get("file_path")
    record["status"] = "not_invoice"
    record["_classification"] = verdict
    record["_not_invoice_reason"] = (
        verdict.get("reason") or "Gemini did not recognise this as an invoice."
    )
    record["_document_type"] = verdict.get("document_type")
    record["_vendor_approved"] = vendor_approved
    record.pop("_extraction_error", None)
    return _hold_as_rejected(record, "not_invoice")


def persist_approved_vendor(
    stored_file_name: str,
    original_name: str,
    user_id=None,
    *,
    skip_invoice_check: bool = False,
) -> dict:
    """
    Register the vendor the admin approved and save the already-extracted invoice.

    Reuses the record from enriched.json so extraction is not run again.
    Unless skip_invoice_check is set (admin already overrode a not-invoice
    verdict), classify the document first. A non-invoice is held for a
    proceed/stop decision and the vendor is not created yet.
    """
    from vim.extraction.json_store import find_by_stored_name

    logger.info("[VENDOR-APPROVE] user_id=%s approving vendor for '%s'", user_id, stored_file_name)
    record = find_by_stored_name(stored_file_name)
    if record is None:
        logger.error("[VENDOR-APPROVE] No enriched.json record found for '%s'", stored_file_name)
        raise ValueError("Extracted data for that upload is no longer available.")

    saved_path = resolve_pending_upload(stored_file_name)
    if saved_path is None:
        saved_path = Path(record.get("file_path") or stored_file_name)

    display_name = original_name or record.get("file_name") or stored_file_name

    if not skip_invoice_check:
        logger.info("[VENDOR-APPROVE] Classifying '%s' before registering vendor", display_name)
        verdict = _classify_held_record(saved_path, display_name, record)
        if verdict.get("error"):
            logger.warning(
                "[VENDOR-APPROVE] Classification unavailable for '%s': %s — continuing",
                display_name, verdict["error"],
            )
        elif verdict.get("is_invoice") is False:
            logger.info(
                "[VENDOR-APPROVE] '%s' is not an invoice (%s) — holding for proceed/stop",
                display_name, verdict.get("document_type"),
            )
            return _hold_not_invoice_keep_extraction(
                record, display_name, saved_path, verdict, vendor_approved=True,
            )

        held = _hold_if_purchase_order(
            record, display_name, saved_path, vendor_approved=True
        )
        if held is not None:
            return held

    if skip_invoice_check or record.get("status") in (
        "not_invoice",
        "vendor_not_registered",
    ):
        record.pop("_pending_vendor", None)
        if record.get("status") in ("not_invoice", "vendor_not_registered"):
            record["status"] = None

    return _persist_record(
        record,
        display_name,
        saved_path,
        register_new_vendor=True,
        decided_by=user_id,
    )
 
 
def _persist_record(
    record: dict,
    original_name: str,
    saved_path: Path,
    *,
    register_new_vendor: bool = False,
    decided_by=None,
) -> dict:
    """Attach filenames, resolve the vendor, and write JSON + SQLite."""
    from vim.extraction.json_store import upsert_record

    logger.info("[PERSIST] Starting persist for '%s' (register_vendor=%s)",
                original_name, register_new_vendor)
    record["file_name"] = original_name
    record["stored_file_name"] = saved_path.name if saved_path else record.get("stored_file_name")

    if record.get("_extraction_error"):
        logger.warning("[PERSIST] Skipping DB save for '%s' — extraction error: %s",
                       original_name, record["_extraction_error"])
        record["status"] = "extraction_failed"
        upsert_record(record)
        return record

    if not str(record.get("vendor_name") or "").strip():
        return _hold_incomplete_header(record, original_name, saved_path)

    # SQLite takes one writer at a time. Serialising this costs almost
    # nothing because the slow work has already finished.
    with _db_write_lock:
        vendor, vendor_action = find_or_create_vendor(
            record, create=register_new_vendor
        )

        if not vendor:
            db.session.rollback()
            # Extracted, but the vendor is new. Hold the invoice until the
            # admin chooses to register them or discard this upload.
            logger.info("[PERSIST] Unknown vendor '%s' on '%s' — holding for admin decision",
                        record.get("vendor_name"), original_name)
            record["vendor_id"] = None
            record["status"] = "vendor_not_registered"
            record["_pending_vendor"] = True
            record.pop("_extraction_error", None)
            return _hold_as_rejected(record, "vendor_not_registered")

        logger.info("[PERSIST] Vendor resolved: '%s' (action=%s)",
                    vendor.VendorName, vendor_action)
        record["vendor_name"] = vendor.VendorName
        record["vendor_id"] = vendor.VendorID
        record["_vendor_action"] = vendor_action
        record.pop("_pending_vendor", None)

        try:
            invoice = insert_record(record)
            db.session.commit()
            record["invoice_id"] = invoice.InvoiceID
            logger.info("[PERSIST] Invoice saved: InvoiceID=%s for '%s'",
                        invoice.InvoiceID, original_name)
            from vim.extraction.rejections import DECISION_PROCEEDED, mark_decision
            mark_decision(
                record.get("stored_file_name"),
                DECISION_PROCEEDED,
                user_id=decided_by,
            )
        except Exception as e:
            db.session.rollback()
            logger.error("[PERSIST] DB error saving '%s': %s", original_name, e, exc_info=True)
            record["status"] = "db_error"
            record["_db_error"] = str(e)
            upsert_record(record)
            return record
 
    record["status"] = _resolve_status(record)
    upsert_record(record)
    return record
 
