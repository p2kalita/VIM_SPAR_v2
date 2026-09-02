import re
from datetime import datetime

from vim.extraction.field_aliases import promote_extra_fields
from vim.extraction.schema import (
    HEADER_FIELDS,
    DATE_FIELDS,
    DOCUMENT_TYPE_CODES,
    MONEY_FIELDS,
    PERCENT_FIELDS,
    LINE_ITEM_FIELDS,
    LINE_ITEM_MONEY_FIELDS,
    LINE_ITEM_PERCENT_FIELDS,
    empty_field_confidence,
    empty_line_item_confidence,
)
from vim_logger import get_logger

logger = get_logger("vim.extraction.validator")
 
 
_DATE_FORMATS = [
    "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y",
    "%m-%d-%Y", "%d/%m/%Y",
]
 
_MONEY_STRIP_RE = re.compile(r"[^\d.\-]")
 
 
def _coerce_date(value):
    if value in (None, "", "null"):
        return None, True
    if isinstance(value, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", value.strip()):
        return value.strip(), True
    if isinstance(value, str):
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(value.strip(), fmt).strftime("%Y-%m-%d"), True
            except ValueError:
                continue
    return value, False
 
 
def _coerce_money(value):
    if value in (None, "", "null"):
        return None, True
    if isinstance(value, (int, float)):
        return float(value), True
    if isinstance(value, str):
        stripped = value.strip()
        # Accounting parenthetical notation: (123.45) means -123.45
        is_parenthetical = stripped.startswith("(") and stripped.endswith(")")
        if is_parenthetical:
            stripped = stripped[1:-1].strip()
        is_credit = is_parenthetical or "-" in stripped or stripped.upper().endswith("CR")
        cleaned = _MONEY_STRIP_RE.sub("", stripped)
        if cleaned in ("", "-"):
            return value, False
        try:
            num = float(cleaned)
            if is_credit and num > 0:
                num = -num
            return num, True
        except ValueError:
            return value, False
    return value, False
 
 
def _coerce_percent(value):
    """Parse a tax rate into a plain number: "18%", "18,5", 18 -> 18 / 18.5."""
    if value in (None, "", "null"):
        return None, True
    if isinstance(value, bool):
        return value, False
    if isinstance(value, (int, float)):
        return float(value), True
    if isinstance(value, str):
        cleaned = value.strip().rstrip("%").strip().replace(",", ".")
        cleaned = _MONEY_STRIP_RE.sub("", cleaned)
        if cleaned in ("", "-", "."):
            return value, False
        try:
            return float(cleaned), True
        except ValueError:
            return value, False
    return value, False
 
 
_DOCUMENT_TYPE_KEYWORDS = [
    (2, ("credit memo", "credit note", "creditnote", "refund")),
    (1, ("debit memo", "debit note", "debitnote")),
    (0, ("tax invoice", "invoice", "bill of supply", "bill")),
]
 
 
def _coerce_document_type_code(value, label):
    """Normalise document_type_code to 0-3, falling back to the printed label."""
    if isinstance(value, bool):
        value = None
 
    if value not in (None, "", "null"):
        try:
            code = int(float(str(value).strip()))
        except (TypeError, ValueError):
            code = None
        if code in DOCUMENT_TYPE_CODES:
            return code, True
        if code is not None:
            return None, False
 
    text = str(label or "").strip().lower()
    if text:
        for code, keywords in _DOCUMENT_TYPE_KEYWORDS:
            if any(k in text for k in keywords):
                return code, True
        return 3, True
 
    return None, True
 
 
def _normalize_item_type(value):
    """Collapse free-text item types onto goods/service."""
    if value in (None, "", "null"):
        return None
    text = str(value).strip().lower()
    if text in ("goods", "good", "material", "materials", "product", "products", "item"):
        return "goods"
    if text in ("service", "services", "labour", "labor", "fee", "fees", "subscription"):
        return "service"
    return str(value).strip()
 
 
# Older records and the LLM both use two names for the same total. Whichever
# side is present fills the other so existing consumers keep working.
_AMOUNT_SYNONYMS = [
    ("total_due", "gross_amount"),
    ("subtotal", "net_amount"),
    ("tax_total", "tax_amount"),
]
 
 
def _reconcile_amounts(record: dict) -> None:
    for left, right in _AMOUNT_SYNONYMS:
        left_value, right_value = record.get(left), record.get(right)
        if left_value is None and right_value is not None:
            record[left] = right_value
        elif right_value is None and left_value is not None:
            record[right] = left_value
 
 
def _looks_like_address(value) -> bool:
    if not isinstance(value, str):
        return False
    return bool(re.search(r"\d", value)) and bool(re.search(r"[A-Za-z]{3,}", value)) and " " in value
 
 
def _clamp_confidence(value) -> int | None:
    if value in (None, "", "null"):
        return None
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if score < 0:
        return 0
    if score > 100:
        return 100
    return int(round(score))
 
 
def _penalize_confidence(confidence_map: dict, field: str, cap: int = 50) -> None:
    current = confidence_map.get(field)
    if current is None:
        confidence_map[field] = cap
    else:
        confidence_map[field] = min(current, cap)
 
 
def _validate_confidence(record: dict, issues: list[str]) -> None:
    field_conf = record.get("field_confidence")
    if not isinstance(field_conf, dict):
        field_conf = empty_field_confidence()
        issues.append("field_confidence was missing — initialized")
    record["field_confidence"] = field_conf
 
    for field in HEADER_FIELDS:
        raw_score = field_conf.get(field)
        field_conf[field] = _clamp_confidence(raw_score)
        if record.get(field) is None:
            field_conf[field] = 0 if raw_score is not None else None
 
    line_conf = record.get("line_items_confidence")
    if not isinstance(line_conf, list):
        line_conf = []
        issues.append("line_items_confidence was not a list — reset to []")
    record["line_items_confidence"] = line_conf
 
    line_items = record.get("line_items") or []
    while len(line_conf) < len(line_items):
        line_conf.append(empty_line_item_confidence())
    record["line_items_confidence"] = line_conf[: len(line_items)]
 
    for idx, item in enumerate(line_items):
        item_conf = line_conf[idx] if idx < len(line_conf) else empty_line_item_confidence()
        if not isinstance(item_conf, dict):
            item_conf = empty_line_item_confidence()
            line_conf[idx] = item_conf
        for key in LINE_ITEM_FIELDS:
            raw_score = item_conf.get(key)
            item_conf[key] = _clamp_confidence(raw_score)
            if item.get(key) is None:
                item_conf[key] = 0 if raw_score is not None else None
 
    extra_conf = record.get("extra_fields_confidence")
    if not isinstance(extra_conf, dict):
        extra_conf = {}
        issues.append("extra_fields_confidence was missing — initialized")
    record["extra_fields_confidence"] = extra_conf
 
    extra_fields = record.get("extra_fields") or {}
    for key in list(extra_conf.keys()):
        if key not in extra_fields:
            del extra_conf[key]
    for key in extra_fields:
        raw_score = extra_conf.get(key)
        extra_conf[key] = _clamp_confidence(raw_score)
        if extra_fields.get(key) is None:
            extra_conf[key] = 0 if raw_score is not None else None
 
    for issue in issues:
        for field in HEADER_FIELDS:
            if issue.startswith(f"{field}:") or issue.startswith(f"missing header field '{field}'"):
                _penalize_confidence(field_conf, field)
        for field in DATE_FIELDS + MONEY_FIELDS + PERCENT_FIELDS:
            if issue.startswith(f"{field}:"):
                _penalize_confidence(field_conf, field)
        if issue.startswith("account_number"):
            _penalize_confidence(field_conf, "account_number")
        if issue.startswith("total_due"):
            _penalize_confidence(field_conf, "total_due")
 
        match = re.match(r"line_items\[(\d+)\]\.(\w+):", issue)
        if match:
            idx, key = int(match.group(1)), match.group(2)
            if idx < len(line_conf) and key in LINE_ITEM_FIELDS:
                _penalize_confidence(line_conf[idx], key)
 
 
def validate_record(record: dict) -> tuple[dict, list[str]]:
    issues: list[str] = []
 
    for field in HEADER_FIELDS:
        if field not in record:
            record[field] = None
            issues.append(f"missing header field '{field}' — set to null")
 
    # Flatten and re-home extra_fields before anything is coerced, so a value
    # promoted out of a vendor-specific label (an "Overdue Date", say) still
    # goes through the same date and money normalisation as the rest.
    extra = record.get("extra_fields")
    if extra is None:
        record["extra_fields"] = {}
    elif not isinstance(extra, dict):
        issues.append(f"extra_fields was not an object (got {type(extra).__name__}) — reset to {{}}")
        record["extra_fields"] = {}
    else:
        flat_extra = {}
        for k, v in extra.items():
            if isinstance(v, (dict, list)):
                flat_extra[k] = str(v)
                issues.append(f"extra_fields.{k} was nested — flattened to string")
            else:
                flat_extra[k] = v
        record["extra_fields"] = flat_extra
 
    issues.extend(promote_extra_fields(record))
 
    for field in DATE_FIELDS:
        coerced, ok = _coerce_date(record.get(field))
        record[field] = coerced
        if not ok:
            issues.append(f"{field}: could not parse date from {record.get(field)!r}")
 
    for field in MONEY_FIELDS:
        coerced, ok = _coerce_money(record.get(field))
        record[field] = coerced
        if not ok:
            issues.append(f"{field}: could not parse number from {record.get(field)!r}")
 
    for field in PERCENT_FIELDS:
        coerced, ok = _coerce_percent(record.get(field))
        record[field] = coerced
        if not ok:
            issues.append(f"{field}: could not parse percentage from {record.get(field)!r}")
 
    _reconcile_amounts(record)
 
    code, ok = _coerce_document_type_code(
        record.get("document_type_code"), record.get("document_type")
    )
    if not ok:
        issues.append(
            f"document_type_code: {record.get('document_type_code')!r} is not one of "
            f"{sorted(DOCUMENT_TYPE_CODES)} — set to null"
        )
    record["document_type_code"] = code
    if not record.get("document_type") and code is not None:
        record["document_type"] = DOCUMENT_TYPE_CODES[code]
 
    if not record.get("po_non_po"):
        record["po_non_po"] = "PO" if record.get("po_number") else "Non-PO"
 
    if record.get("total_due") is None:
        issues.append("total_due is null — check source document / vision output")
 
    if _looks_like_address(record.get("account_number")):
        issues.append(
            f"account_number looks like an address, not an account number: "
            f"{record['account_number']!r}"
        )
 
    line_items = record.get("line_items")
    if line_items is None:
        record["line_items"] = []
    elif not isinstance(line_items, list):
        issues.append(f"line_items was not a list (got {type(line_items).__name__}) — reset to []")
        record["line_items"] = []
    else:
        cleaned_items = []
        for idx, item in enumerate(line_items):
            if not isinstance(item, dict):
                issues.append(f"line_items[{idx}] was not an object — skipped")
                continue
            for key in LINE_ITEM_FIELDS:
                item.setdefault(key, None)
            for money_key in LINE_ITEM_MONEY_FIELDS:
                coerced, ok = _coerce_money(item.get(money_key))
                item[money_key] = coerced
                if not ok:
                    issues.append(f"line_items[{idx}].{money_key}: could not parse {item.get(money_key)!r}")
            for percent_key in LINE_ITEM_PERCENT_FIELDS:
                coerced, ok = _coerce_percent(item.get(percent_key))
                item[percent_key] = coerced
                if not ok:
                    issues.append(
                        f"line_items[{idx}].{percent_key}: could not parse "
                        f"percentage from {item.get(percent_key)!r}"
                    )
            item["item_type"] = _normalize_item_type(item.get("item_type"))
            cleaned_items.append(item)
        record["line_items"] = cleaned_items
 
    if not record.get("currency"):
        record["currency"] = "USD"
 
    _validate_confidence(record, issues)
 
    return record, issues
 
 