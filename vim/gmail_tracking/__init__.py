"""
Gmail ingest outcome tracking: which emailed attachments became invoices.

Self-contained: the only hook into the app is init_gmail_tracking(app),
called from create_app() after register_routes(app) and before
db.create_all() so the new tables are created.
"""

from vim_logger import get_logger

logger = get_logger("vim.gmail_tracking")


def init_gmail_tracking(app) -> None:
    """Register models, the dashboard blueprint, and start the reconciler."""
    from vim.gmail_tracking import models  # noqa: F401  (registers tables)
    from vim.gmail_tracking.routes import bp
    from vim.gmail_tracking.reconciler import start_reconciler

    if bp.name not in app.blueprints:
        app.register_blueprint(bp)
        logger.info("[GMAIL-TRACKING] Dashboard registered at /admin/ingest-outcomes")

    try:
        start_reconciler(app)
    except Exception:
        # Tracking must never stop the app from starting.
        logger.exception("[GMAIL-TRACKING] Could not start reconciler")
