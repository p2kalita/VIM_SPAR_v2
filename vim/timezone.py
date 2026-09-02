"""
vim/timezone.py
─────────────────────────────────────────────────────────────────────────────
Central timezone utility configured for Indian Standard Time (IST, UTC+05:30).
─────────────────────────────────────────────────────────────────────────────
"""

from datetime import datetime, timezone, timedelta

# Indian Standard Time (IST): UTC + 5 hours 30 minutes
IST = timezone(timedelta(hours=5, minutes=30), name="IST")


def get_ist_now() -> datetime:
    """Return current naive datetime in Indian Standard Time (IST) for database storage."""
    return datetime.now(IST).replace(tzinfo=None)


def get_ist_now_aware() -> datetime:
    """Return current timezone-aware datetime in Indian Standard Time (IST)."""
    return datetime.now(IST)


def to_ist(dt: datetime) -> datetime:
    """Convert any aware datetime to IST, or attach IST if naive."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def format_ist(dt: datetime, fmt: str = "%Y-%m-%d %H:%M:%S IST") -> str:
    """Format a datetime in Indian Standard Time."""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        return dt.strftime(fmt)
    return dt.astimezone(IST).strftime(fmt)
