"""Background Gmail poller. Starts once per process and feeds the ingest pipeline."""

from __future__ import annotations

import threading
import time

from vim_logger import get_logger

logger = get_logger("vim.gmail.poller")

_started = False
_lock = threading.Lock()


def start_poller(app) -> None:
    global _started
    with _lock:
        if _started:
            return
        _started = True
    thread = threading.Thread(
        target=_loop,
        args=(app,),
        name="gmail-poller",
        daemon=True,
    )
    thread.start()
    logger.info("[GMAIL] Poller started")


def _loop(app) -> None:
    from vim.gmail.service import default_poll_seconds, get_account, sync_inbox

    logger.info("[GMAIL] Poller loop running")
    while True:
        wait = default_poll_seconds()
        started = time.time()
        try:
            enabled = False
            with app.app_context():
                row = get_account()
                if row and row.PollSeconds:
                    wait = max(30, min(int(row.PollSeconds), 3600))
                enabled = bool(row and row.TokenJson and row.Enabled is not False)
            if enabled:
                logger.info("[GMAIL] Poller checking inbox")
                summary = sync_inbox(app)
                logger.info(
                    "[GMAIL] Poller check finished ingested=%s skipped=%s errors=%s",
                    summary.get("ingested"),
                    summary.get("skipped"),
                    summary.get("errors"),
                )
        except Exception:
            logger.exception("[GMAIL] Poller cycle failed")
        elapsed = time.time() - started
        time.sleep(max(1.0, wait - elapsed))
