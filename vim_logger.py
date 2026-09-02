"""
vim_logger.py
─────────────────────────────────────────────────────────────────────────────
Central logging configuration for the Vendor Invoice Management system.
Timestamps formatted in Indian Standard Time (IST, UTC+05:30).

Usage in any module:
    from vim_logger import get_logger
    logger = get_logger(__name__)

Log file:  logs/vim.log   (rotates at 5 MB, keeps 5 backups)
Console:   always on (INFO+)
File:      always on (DEBUG+) — full detail including LLM calls
─────────────────────────────────────────────────────────────────────────────
"""

import logging
import sys
from datetime import datetime, timezone, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Indian Standard Time (IST)
_IST = timezone(timedelta(hours=5, minutes=30))


class ISTFormatter(logging.Formatter):
    """Custom logging formatter outputting timestamps in Indian Standard Time (IST)."""

    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, _IST)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime("%Y-%m-%d %H:%M:%S")


# ── Paths ──────────────────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent
_LOG_DIR      = _PROJECT_ROOT / "logs"
_LOG_FILE     = _LOG_DIR / "vim.log"

# ── Formats ────────────────────────────────────────────────────────────────
_FMT_CONSOLE = (
    "%(asctime)s  %(levelname)-8s  [%(name)s]  %(message)s"
)
_FMT_FILE = (
    "%(asctime)s  %(levelname)-8s  [%(name)s]  %(filename)s:%(lineno)d  %(message)s"
)
_DATE_FMT = "%Y-%m-%d %H:%M:%S"

# ── One-time setup flag ─────────────────────────────────────────────────────
_configured = False


def _setup() -> None:
    global _configured
    if _configured:
        return
    _configured = True

    _LOG_DIR.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger("vim")
    root.setLevel(logging.DEBUG)          # capture everything; handlers filter

    # ── Console handler (INFO+) ─────────────────────────────────────────────
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(ISTFormatter(_FMT_CONSOLE, datefmt=_DATE_FMT))
    root.addHandler(ch)

    # ── Rotating file handler (DEBUG+) ──────────────────────────────────────
    fh = RotatingFileHandler(
        _LOG_FILE,
        maxBytes=5 * 1024 * 1024,   # 5 MB
        backupCount=5,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(ISTFormatter(_FMT_FILE, datefmt=_DATE_FMT))
    root.addHandler(fh)

    root.propagate = False


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the 'vim' namespace."""
    _setup()
    # Ensure the name is always under the vim namespace
    if not name.startswith("vim"):
        name = f"vim.{name}"
    return logging.getLogger(name)
