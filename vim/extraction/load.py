"""Persist extracted invoice records into the VIM database."""
 
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
 
from vim_database.database import db
from vim_database.models import (
    Vendor,
    PurchaseOrder,
    Invoice,
    InvoiceDocument,
    InvoiceLineItem,
    OCRExtraction,
)
from vim_logger import get_logger
 
logger = get_logger("vim.extraction.load")
 
 
def _get(record: dict, field, default=None):
    v = record.get(field)
    if v not in (None, ""):
        return v
    return default
 
 
def _fit(record: dict, field, length: int, default=None):
    """Read a text field and trim it to its column width."""
    value = _get(record, field)
    if value in (None, ""):
        return default
    return str(value).strip()[:length] or default
 
 
def _get_date(record: dict, field, default=None):
    v = _get(record, field, default=None)
    if v is None:
        return default
    if isinstance(v, date):
        return v
    try:
        return datetime.strptime(v, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return default
 
 
def _confidence_score(record: dict) -> Decimal:
    if record.get("_extraction_error"):
        return Decimal("0.00")
 
    scores = [
        v for v in (record.get("field_confidence") or {}).values()
        if isinstance(v, (int, float))
    ]
    if scores:
        return Decimal(str(round(sum(scores) / len(scores), 2)))
 
    issues = record.get("_validation_issues") or []
    if not issues:
        return Decimal("95.00")
    if len(issues) <= 2:
        return Decimal("75.00")
    return Decimal("50.00")
 
 
def _extraction_status(record: dict) -> str:
    if record.get("_extraction_error"):
        return "Failed"
    if record.get("_validation_issues"):
        return "NeedsReview"
    return "Success"
 
 
def _find_vendor(record: dict) -> Vendor:
    vendor_name = _get(record, "vendor_name")
    if not vendor_name:
        raise ValueError("vendor_name is required")
 
    logger.debug("[LOAD] Looking up vendor '%s'", vendor_name)
    vendor = Vendor.query.filter(
        db.func.lower(Vendor.VendorName) == str(vendor_name).strip().lower(),
        Vendor.Status == 1,
    ).first()
    if vendor:
        logger.debug("[LOAD] Vendor found by name: VendorID=%s", vendor.VendorID)
        return vendor
 
    vendor_id = record.get("vendor_id")
    if vendor_id:
        vendor = db.session.get(Vendor, vendor_id)
        if vendor and vendor.Status == 1:
            logger.debug("[LOAD] Vendor found by ID: VendorID=%s", vendor.VendorID)
            return vendor
 
    logger.warning("[LOAD] Vendor '%s' not registered", vendor_name)
    raise ValueError(f"Vendor {vendor_name!r} is not registered")
 
 
def _find_or_create_purchase_order(record: dict, vendor: Vendor, file_name: str) -> PurchaseOrder:
    po_number = _get(record, "po_number") or f"AUTO-{file_name}"
 
    po = PurchaseOrder.query.filter_by(PONumber=po_number).first()
    if po:
        logger.debug("[LOAD] Existing PO found: '%s'", po_number)
        return po
 
    logger.info("[LOAD] Creating new PO: '%s' for VendorID=%s", po_number, vendor.VendorID)
    po = PurchaseOrder(
        PONumber=po_number,
        VendorID=vendor.VendorID,
        PODate=_get_date(record, "invoice_date", default=date(1970, 1, 1)),
        TotalAmount=_get(record, "total_due", default=0) or 0,
        Status=_get(record, "invoice_status", default="Open"),
    )
    db.session.add(po)
    db.session.flush()
    return po
 
 
def _upsert_ocr_extraction(document: InvoiceDocument, record: dict) -> None:
    ocr = document.ocr_extraction
    if not ocr:
        ocr = OCRExtraction(DocumentID=document.DocumentID)
        db.session.add(ocr)
 
    ocr.ExtractedVendorName = _get(record, "vendor_name", default="Unknown") or "Unknown"
    ocr.ExtractedInvoiceNumber = _get(record, "invoice_number", default="") or ""
    ocr.ExtractedInvoiceDate = _get_date(record, "invoice_date", default=date(1970, 1, 1))
    ocr.ExtractedAmount = _get(record, "total_due", default=0) or 0
    ocr.ConfidenceScore = _confidence_score(record)
    ocr.ExtractionStatus = _extraction_status(record)
 
 
def insert_record(record: dict) -> Invoice:
    """Insert or update invoice data from an enriched extraction record."""
    file_name = record.get("file_name")
    logger.info("[LOAD] insert_record called for file='%s', invoice_number='%s'",
                file_name, record.get("invoice_number"))
 
    vendor = _find_vendor(record)
    po = _find_or_create_purchase_order(record, vendor, file_name)
 
    invoice_fields = dict(
        InvoiceNumber=_get(record, "invoice_number", default="") or f"UNKNOWN-{file_name}",
        InvoiceDate=_get_date(record, "invoice_date", default=date(1970, 1, 1)),
        VendorID=vendor.VendorID,
        PONumber=po.PONumber,
        InvoiceAmount=_get(record, "total_due", default=0) or 0,
        Currency=_get(record, "currency", default="USD"),
        DueDate=_get_date(record, "due_date", default=date(1970, 1, 1)),
        InvoiceStatus=_get(record, "invoice_status", default="Pending"),
 
        # Everything below is optional on the document; null means "not printed".
        DocumentTypeCode=_get(record, "document_type_code"),
        DocumentType=_fit(record, "document_type", 50),
        PONonPO=_fit(record, "po_non_po", 10),
 
        NetAmount=_get(record, "net_amount"),
        TaxAmount=_get(record, "tax_amount"),
        TaxRate=_get(record, "tax_rate"),
        FreightAmount=_get(record, "freight_amount"),
        GrossAmount=_get(record, "gross_amount"),
 
        OrderDate=_get_date(record, "order_date"),
        DeliveryDate=_get_date(record, "delivery_date"),
        PaymentTerms=_fit(record, "payment_terms", 50),
        OriginalInvoiceNumber=_fit(record, "original_invoice_number", 50),
        CustomerNote=_fit(record, "customer_note", 500),
 
        VendorVATNumber=_fit(record, "vendor_vat_number", 50),
        BuyerVATNumber=_fit(record, "buyer_vat_number", 50),
        RecipientNumber=_fit(record, "recipient_number", 50),
        RequestorName=_fit(record, "requestor_name", 100),
        PaymentReference=_fit(record, "payment_reference", 100),
        Language=_fit(record, "language", 20),
        CountryCode=_fit(record, "country_code", 10),
        RemitToPostalCode=_fit(record, "remit_to_postal_code", 20),
 
        BankName=_fit(record, "bank_name", 100),
        BankAccountNumber=_fit(record, "bank_account_number", 50),
        BankKey=_fit(record, "bank_key", 50),
        IBAN=_fit(record, "iban", 50),
        IFSCCode=_fit(record, "ifsc_code", 20),
    )
 
    # Match this physical upload, not every file that shares an original name.
    # Bulk batches often repeat "invoice.pdf"; matching on FileName alone
    # overwrote one invoice row and left validation looking like a single file.
    storage_path = (record.get("file_path") or "").strip()
    existing_doc = (
        InvoiceDocument.query.filter_by(StoragePath=storage_path).first()
        if storage_path
        else None
    )
 
    if existing_doc:
        invoice = db.session.get(Invoice, existing_doc.InvoiceID)
        for key, value in invoice_fields.items():
            setattr(invoice, key, value)
        document = existing_doc
    else:
        invoice = Invoice(**invoice_fields)
        db.session.add(invoice)
        db.session.flush()
 
        document = InvoiceDocument(
            InvoiceID=invoice.InvoiceID,
            FileName=file_name,
            FileType="",
            StoragePath="",
        )
        db.session.add(document)
        db.session.flush()
 
    document.FileType = Path(file_name or "").suffix.lstrip(".").upper()
    document.StoragePath = record.get("file_path") or ""
 
    InvoiceLineItem.query.filter_by(InvoiceID=invoice.InvoiceID).delete()
    for item in record.get("line_items") or []:
        db.session.add(InvoiceLineItem(
            InvoiceID=invoice.InvoiceID,
            Description=_get(item, "description", default=""),
            Quantity=_get(item, "quantity", default=0) or 0,
            CostAmount=_get(item, "unit_price", default=0) or 0,
            DiscountAmount=0,
            LineAmount=_get(item, "amount", default=0) or 0,
            ItemType=_fit(item, "item_type", 20),
            UnitOfMeasure=_fit(item, "unit_of_measure", 20),
            TaxRate=_get(item, "tax_rate"),
            TaxAmount=_get(item, "tax_amount"),
            PONumber=_fit(item, "po_number", 50),
        ))
 
    _upsert_ocr_extraction(document, record)
    return invoice