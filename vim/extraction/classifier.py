"""Gemini-backed gate that decides whether an upload is really an invoice."""

import io
import json
import re
import time
from pathlib import Path

from vim.extraction import config
from vim_logger import get_logger

logger = get_logger("vim.extraction.classifier")

_gemini_client = None

# Text beyond this point rarely changes the verdict and only costs tokens.
_MAX_CHARS = 12000

# Gemini 3.x spends output tokens on internal reasoning before it answers, so a
# small cap truncates the JSON mid-string. This leaves room for both.
_MAX_OUTPUT_TOKENS = 2048

_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 1.5

# Formats the Gemini API accepts directly; anything else is converted to JPEG.
_NATIVE_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

_MAX_IMAGE_EDGE = 1600

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "is_invoice": {"type": "boolean"},
        "document_type": {"type": "string"},
        "confidence": {"type": "integer"},
        "reason": {"type": "string"},
    },
    "required": ["is_invoice", "document_type", "confidence", "reason"],
}

_CRITERIA = """
Decide whether this document is a VENDOR INVOICE or an equivalent billable
document (invoice, tax invoice, credit note, debit note, or a utility/telecom
statement sent to a company accounts-payable team).

It is NOT an invoice if it is any of:
- a BILL OF SUPPLY (also labelled "Bill of Supplies")
- a medical or pharmacy PRESCRIPTION, RX slip, or RX copay
- a retail / pharmacy till receipt or card-terminal (chip/debit/credit) receipt
- a purchase order (PO), quote, sales order
- a contract, resume, report, presentation, specification, email, letter
- a screenshot, photograph, logo, chart, bank statement, marketing material
- any other document that does not request payment from a company for goods
  or services already (or being) supplied on a vendor invoice

A Bill of Supply is a GST document used when tax is not charged. It is not
a tax invoice and must be rejected, even if it has a supplier name, line
items, and amounts.

A pharmacy "combined sale", prescription, or card-chip receipt is a consumer
checkout slip, not a vendor invoice, even if it has line items and amounts.

A purchase order is a buyer's request to supply goods or services. It is
not a vendor invoice even if it has line items, amounts, a "Sales Invoice
Template" header, or the word "invoice" on the page.

Rules:
- Base the decision only on the content shown.
- Mentioning the words "invoice" or "payment" is not enough on its own; look
  for a real vendor-invoice structure such as an invoice number, billed
  amounts, line items, or a payment request to an AP department.
- If the document is titled BILL OF SUPPLY / BILL OF SUPPLIES, it is not
  an invoice. Set is_invoice to false.
- If the document is a prescription, RX copay, or POS/card receipt, it is
  not an invoice — even when it shows a store name, totals, and tax.
- If the document is titled PURCHASE ORDER, or it has a PO number but no
  invoice number and no invoice date, it is not an invoice.
- "document_type" is a short human-readable label, e.g. "Tax Invoice",
  "Bill of Supply", "Purchase Order", "Prescription", or "Retail receipt".
- "confidence" is an integer from 0 to 100.
- "reason" is one short sentence explaining the decision.
""".strip()

_TEXT_PROMPT = (
    "You are a document triage step in an invoice processing system.\n\n"
    + _CRITERIA
)

_IMAGE_PROMPT = (
    "You are a document triage step in an invoice processing system. "
    "Look at the attached image.\n\n"
    + _CRITERIA
)


def _get_gemini_client():
    """Build (once) a google-genai client from the key in .env."""
    global _gemini_client
    if _gemini_client is None:
        from google import genai

        api_key = config.read_gemini_key()
        if not api_key:
            logger.error("[CLASSIFY] GEMINI_API_KEY is not set in environment or .env")
            raise EnvironmentError(
                "GEMINI_API_KEY is not set. Add it to .env and restart the server."
            )
        logger.info("[CLASSIFY] Initialising Gemini client with model=%s", config.GEMINI_MODEL)
        _gemini_client = genai.Client(api_key=api_key)
    return _gemini_client


def _strip_fences(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[: text.rfind("```")]
    return text.strip()


def _short_error(error: Exception, limit: int = 200) -> str:
    """Collapse verbose API errors — a 502 arrives as a full HTML page."""
    text = re.sub(r"<[^>]+>", " ", str(error))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("..." if len(text) > limit else "")


def _blank_verdict() -> dict:
    return {
        "is_invoice": None,
        "document_type": None,
        "confidence": None,
        "reason": None,
        "error": None,
        "model": config.GEMINI_MODEL,
    }


def _finish_reason(response) -> str:
    try:
        return str(response.candidates[0].finish_reason or "")
    except Exception:
        return ""


def _is_transient(error: Exception) -> bool:
    """Gemini returns occasional 5xx / timeouts that succeed on a retry."""
    text = str(error).lower()
    markers = (
        "502", "503", "504", "500",
        "bad gateway", "unavailable", "deadline", "timeout",
        "internal error", "overloaded", "try again",
    )
    return any(m in text for m in markers)


def _generate(contents):
    """Call Gemini, retrying briefly on transient server errors."""
    last_error = None

    for attempt in range(_MAX_ATTEMPTS):
        try:
            logger.debug("[CLASSIFY] Calling Gemini API (attempt %d/%d)", attempt + 1, _MAX_ATTEMPTS)
            return _get_gemini_client().models.generate_content(
                model=config.GEMINI_MODEL,
                contents=contents,
                config={
                    "temperature": 0.0,
                    "response_mime_type": "application/json",
                    "response_schema": _RESPONSE_SCHEMA,
                    "max_output_tokens": _MAX_OUTPUT_TOKENS,
                },
            )
        except Exception as e:
            last_error = e
            logger.warning("[CLASSIFY] Gemini attempt %d failed: %s", attempt + 1, _short_error(e))
            if attempt == _MAX_ATTEMPTS - 1 or not _is_transient(e):
                raise
            time.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))

    raise last_error


def _ask_gemini(contents) -> dict:
    """Send a classification request and normalise the verdict."""
    blank = _blank_verdict()

    try:
        response = _generate(contents)
    except Exception as e:
        return {**blank, "error": _short_error(e)}

    reason = _finish_reason(response)
    raw = _strip_fences(getattr(response, "text", None) or "")

    if not raw:
        detail = f" (finish_reason={reason})" if reason else ""
        return {**blank, "error": f"empty response from Gemini{detail}"}

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        if "MAX_TOKENS" in reason.upper():
            return {
                **blank,
                "error": (
                    "Gemini response was cut off before the JSON completed "
                    f"(finish_reason={reason}). Raise _MAX_OUTPUT_TOKENS."
                ),
            }
        return {**blank, "error": f"could not parse Gemini response as JSON: {e}"}

    is_invoice = payload.get("is_invoice")
    if not isinstance(is_invoice, bool):
        logger.error("[CLASSIFY] Invalid boolean for is_invoice: %s", payload)
        return {**blank, "error": f"unexpected classifier response: {payload!r}"}

    confidence = payload.get("confidence")
    verdict = {
        "is_invoice": is_invoice,
        "document_type": (payload.get("document_type") or "").strip() or None,
        "confidence": confidence if isinstance(confidence, (int, float)) else None,
        "reason": (payload.get("reason") or "").strip() or None,
        "error": None,
        "model": config.GEMINI_MODEL,
    }
    logger.info("[CLASSIFY] Verdict: is_invoice=%s, doc_type='%s', conf=%s, reason='%s'",
                is_invoice, verdict["document_type"], verdict["confidence"], verdict["reason"])
    return verdict


def classify_document(raw_text: str, file_name: str = "") -> dict:
    """
    Ask Gemini whether raw_text is an invoice.

    Returns is_invoice / document_type / confidence / reason / error. On failure
    error is set and is_invoice is None, so the caller decides whether to block.
    """
    logger.info("[CLASSIFY] Classifying document text for '%s' (%d chars)", file_name, len(raw_text or ""))
    if not (raw_text or "").strip():
        logger.warning("[CLASSIFY] No text available to classify for '%s'", file_name)
        return {**_blank_verdict(), "error": "no text available to classify"}

    return _ask_gemini(
        f"{_TEXT_PROMPT}\n\n"
        f"--- DOCUMENT ({file_name}) ---\n{raw_text[:_MAX_CHARS]}"
    )


def _image_part(file_path: Path):
    """Build a Gemini image part, converting formats it cannot read."""
    from google.genai import types
    from PIL import Image

    suffix = file_path.suffix.lower()
    native_mime = _NATIVE_IMAGE_MIME.get(suffix)

    with Image.open(file_path) as img:
        if getattr(img, "n_frames", 1) > 1:
            img.seek(0)

        width, height = img.size
        oversized = max(width, height) > _MAX_IMAGE_EDGE

        # Send untouched when Gemini already understands the format.
        if native_mime and not oversized:
            return types.Part.from_bytes(
                data=file_path.read_bytes(), mime_type=native_mime
            )

        converted = img.convert("RGB")
        if oversized:
            scale = _MAX_IMAGE_EDGE / max(width, height)
            converted = converted.resize(
                (max(1, int(width * scale)), max(1, int(height * scale))),
                Image.LANCZOS,
            )

        buffer = io.BytesIO()
        converted.save(buffer, format="JPEG", quality=90)

    return types.Part.from_bytes(data=buffer.getvalue(), mime_type="image/jpeg")


def classify_image(file_path, file_name: str = "") -> dict:
    """
    Ask Gemini whether an image is an invoice, looking at the image itself.

    Used instead of the text path because OCR usually returns nothing for an
    image that is not a document at all, which would otherwise surface as an
    extraction failure rather than a clear "not an invoice" answer.
    """
    path = Path(file_path)
    logger.info("[CLASSIFY] Classifying image directly for '%s' (%s)", file_name or path.name, path)
    if not path.is_file():
        logger.error("[CLASSIFY] File not found: %s", path)
        return {**_blank_verdict(), "error": f"file not found: {path}"}

    try:
        part = _image_part(path)
    except Exception as e:
        logger.error("[CLASSIFY] Could not read image '%s': %s", path.name, e, exc_info=True)
        return {**_blank_verdict(), "error": f"could not read image: {e}"}

    return _ask_gemini([f"{_IMAGE_PROMPT}\n\n--- IMAGE ({file_name}) ---", part])


_PO_HEADING = re.compile(r"(?im)^\s*purchase\s+order\s*$")
_PO_IN_TYPE = re.compile(r"purchase\s*order|\bpo\b", re.I)
_PO_FILENAME = re.compile(r"(?i)(^|[^a-z0-9])po[-_\s.]")
_PRESCRIPTION_FILE = re.compile(r"(?i)prescription|\brx[-_\s.]")
_RX_ITEM = re.compile(r"(?i)\b(rx\s*copay|prescription|rx\s*#)\b")
_POS_EXTRA_KEYS = {"card_entry", "card_type", "card_number"}
_BOS_PHRASE = re.compile(r"bill\s+of\s+supplys?", re.I)
_BOS_FILENAME = re.compile(r"(?i)bill[_\s-]*of[_\s-]*supply")


def purchase_order_hold_reason(record: dict) -> str | None:
    """
    Return a hold reason when extracted fields show a purchase order, not an invoice.

    Gemini can miss this when the file is a PO printed on an invoice template.
    A real invoice may reference a PO number, but it still has an invoice number.
    """
    invoice_number = str(record.get("invoice_number") or "").strip()
    po_number = str(record.get("po_number") or "").strip()
    invoice_date = str(record.get("invoice_date") or "").strip()
    document_type = str(record.get("document_type") or "").strip()
    raw_text = record.get("raw_text") or ""
    file_name = str(record.get("file_name") or record.get("stored_file_name") or "")

    if _PO_IN_TYPE.search(document_type) and "invoice" not in document_type.lower():
        return f"Document type is '{document_type}', not an invoice."

    if _PO_HEADING.search(raw_text) and not invoice_number:
        return "The document is labelled PURCHASE ORDER and has no invoice number."

    if po_number and not invoice_number and not invoice_date:
        return (
            f"This looks like purchase order {po_number}: it has a PO number "
            "but no invoice number or invoice date."
        )

    if _PO_FILENAME.search(Path(file_name).name) and not invoice_number:
        return "The file is named as a purchase order and has no invoice number."

    return None


def non_invoice_hold_reason(record: dict) -> tuple[str, str] | None:
    """
    Hold reason when extraction shows a non-invoice (PO, prescription, POS
    receipt) even if Gemini first called it an invoice.

    Returns (reason, document_type_label) or None.
    """
    po_reason = purchase_order_hold_reason(record)
    if po_reason:
        return po_reason, "Purchase Order"

    invoice_number = str(record.get("invoice_number") or "").strip()
    document_type = str(record.get("document_type") or "").strip()
    type_lower = document_type.lower()
    file_name = str(record.get("file_name") or record.get("stored_file_name") or "")
    raw_text = record.get("raw_text") or ""
    extra = record.get("extra_fields") or {}
    extra_keys = {str(k).lower() for k in extra}
    extra_text = " ".join(str(v) for v in extra.values()).lower()
    item_text = " ".join(
        str(item.get("description") or "")
        for item in (record.get("line_items") or [])
        if isinstance(item, dict)
    )

    if (
        _BOS_PHRASE.search(document_type)
        or _BOS_PHRASE.search(raw_text)
        or _BOS_FILENAME.search(Path(file_name).name)
    ):
        return (
            "This is a Bill of Supply, not a tax invoice.",
            "Bill of Supply",
        )

    if _PRESCRIPTION_FILE.search(Path(file_name).name):
        return (
            "The file is a prescription, not a vendor invoice.",
            "Prescription",
        )

    if "prescription" in type_lower:
        return (
            f"Document type is '{document_type}', not a vendor invoice.",
            document_type or "Prescription",
        )

    if _RX_ITEM.search(item_text) and not invoice_number:
        return (
            "This looks like a pharmacy prescription or RX copay slip, "
            "not a vendor invoice.",
            "Prescription / pharmacy receipt",
        )

    looks_pos = bool(_POS_EXTRA_KEYS & extra_keys) or "chip" in extra_text
    if looks_pos and not invoice_number:
        return (
            "This looks like a card-terminal or retail receipt, "
            "not a vendor invoice.",
            "Retail receipt",
        )

    if (
        ("pharmacy" in type_lower or "combined sale" in type_lower)
        and not invoice_number
    ):
        return (
            f"Document type is '{document_type}' with no invoice number.",
            document_type or "Pharmacy receipt",
        )

    return None

