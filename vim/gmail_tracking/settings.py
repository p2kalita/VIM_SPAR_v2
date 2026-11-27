"""Environment settings for Gmail ingest outcome tracking (all optional)."""

from __future__ import annotations

import os


def _int_env(name: str, default: int, low: int, high: int) -> int:
    raw = (os.getenv(name) or "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return max(low, min(value, high))


def tracking_enabled() -> bool:
    """GMAIL_TRACKING=0/false/off disables the background reconciler."""
    raw = (os.getenv("GMAIL_TRACKING") or "1").strip().lower()
    return raw not in ("0", "false", "off", "no")


def poll_seconds() -> int:
    """GMAIL_TRACKING_SECONDS: reconciler interval, default 30, clamped 10-600."""
    return _int_env("GMAIL_TRACKING_SECONDS", 30, 10, 600)


def lookback_days() -> int:
    """GMAIL_TRACKING_LOOKBACK_DAYS: how far back emails are reconciled, default 7."""
    return _int_env("GMAIL_TRACKING_LOOKBACK_DAYS", 7, 1, 365)


def match_hours() -> int:
    """GMAIL_TRACKING_MATCH_HOURS: fallback match window after ingest, default 2."""
    return _int_env("GMAIL_TRACKING_MATCH_HOURS", 2, 1, 72)


# Fallback attempts before an email with no linkable attachment is given up on.
MAX_FALLBACK_ATTEMPTS = 3

# Grace before the ingest row's CreatedDate when matching by time: the
# pipeline can write its outcome before sync_inbox records the email.
MATCH_GRACE_MINUTES = 2
