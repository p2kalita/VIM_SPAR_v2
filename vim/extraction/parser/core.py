"""Turn an uploaded file into text, preferring a local read over LlamaParse.
 
LlamaParse is a cloud OCR round-trip and is typically the slowest step in
the pipeline (20–40s). Native-text files and digital PDFs already contain
selectable text, so sending them to OCR is wasted wait time. Scanned PDFs
and images still fall through to LlamaParse.
"""
 
import time
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
from types import SimpleNamespace
 
from vim.extraction import config
from vim_logger import get_logger
 
logger = get_logger("vim.extraction.parser")
 
# Below this many letters, a PDF is almost certainly a scan, not digital text.
_MIN_DIGITAL_LETTERS = 80
 
_PLAIN_TEXT_EXTENSIONS = {".txt", ".csv", ".html", ".htm"}
_DOCX_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
 
 
def parse_single_file(file_path: str, **overrides) -> list:
    path = Path(file_path)
    if not path.exists():
        logger.error("[PARSER] File not found: %s", path.resolve())
        raise FileNotFoundError(f"File not found: {path.resolve()}")
 
    verbose = overrides.get("verbose", True)
    started = time.perf_counter()
 
    local_text, source = _try_local(path)
    if local_text:
        elapsed = time.perf_counter() - started
        logger.info(
            "[PARSER] Parsed %s locally via %s (%d chars, %.2fs)",
            path.name, source, len(local_text), elapsed
        )
        return [SimpleNamespace(text=local_text)]
 
    logger.info("[PARSER] Parsing %s via LlamaParse (no local text)...", path.name)
 
    from llama_parse import LlamaParse
 
    parser = LlamaParse(
        api_key=config.LLAMA_CLOUD_API_KEY,
        result_type=overrides.get("result_type", config.RESULT_TYPE),
        # fast_mode skips image OCR and table reconstruction. Digital files
        # never reach here; scans still get OCR, just without the slow extras.
        fast_mode=True,
        verbose=verbose,
        num_workers=overrides.get("num_workers", config.NUM_WORKERS),
    )
    documents = parser.load_data(str(path))
    elapsed = time.perf_counter() - started
    logger.info(
        "[PARSER] LlamaParse finished %s — %d document(s) in %.2fs",
        path.name, len(documents), elapsed
    )
    return documents
 
 
def _try_local(path: Path) -> tuple[str | None, str | None]:
    """Return (text, source) when the file can be read without a cloud call."""
    ext = path.suffix.lower()
    try:
        if ext in _PLAIN_TEXT_EXTENSIONS:
            return path.read_text(encoding="utf-8", errors="replace"), ext.lstrip(".")
        if ext == ".pdf":
            return _read_pdf(path), "pypdf"
        if ext == ".docx":
            return _read_docx(path), "docx"
        if ext in {".xlsx", ".xlsm"}:
            text = _read_xlsx(path)
            return (text, "openpyxl") if text else (None, None)
    except Exception as e:
        logger.warning("[PARSER] Local parse of %s failed (%s); falling back to LlamaParse", path.name, e)
        return None, None
    return None, None
 
 
def _read_pdf(path: Path) -> str | None:
    from pypdf import PdfReader
 
    pages = []
    for page in PdfReader(str(path)).pages:
        pages.append(page.extract_text() or "")
    text = "\n\n".join(pages).strip()
    letters = sum(ch.isalnum() for ch in text)
    if letters < _MIN_DIGITAL_LETTERS:
        return None
    return text
 
 
def _read_docx(path: Path) -> str | None:
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml")
 
    root = ET.fromstring(xml)
    lines = []
    for paragraph in root.iter(f"{{{_DOCX_NS['w']}}}p"):
        parts = [
            node.text or ""
            for node in paragraph.iter(f"{{{_DOCX_NS['w']}}}t")
        ]
        line = "".join(parts).strip()
        if line:
            lines.append(line)
    text = "\n".join(lines).strip()
    return text or None
 
 
def _read_xlsx(path: Path) -> str | None:
    try:
        import openpyxl
    except ImportError:
        return None
 
    workbook = openpyxl.load_workbook(path, data_only=True, read_only=True)
    blocks = []
    try:
        for sheet in workbook.worksheets:
            rows = []
            for row in sheet.iter_rows(values_only=True):
                cells = ["" if cell is None else str(cell) for cell in row]
                if any(cell.strip() for cell in cells):
                    rows.append(" | ".join(cells))
            if rows:
                blocks.append(f"# {sheet.title}\n" + "\n".join(rows))
    finally:
        workbook.close()
    text = "\n\n".join(blocks).strip()
    return text or None
 
 