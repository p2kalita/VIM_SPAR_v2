"""Admin dashboard for Gmail ingest outcomes (the pending invoice dashboard)."""

from __future__ import annotations

import math

from flask import Blueprint, render_template, request

from vim.gmail_tracking import outcomes
from vim.rbac import Permission, permission_required

PER_PAGE = 50

bp = Blueprint("gmail_tracking", __name__)

_BADGE = {
    outcomes.PROCESSED: "badge-approved",
    outcomes.PENDING: "badge-pending",
    outcomes.ERROR: "badge-failed",
}


@bp.route("/admin/ingest-outcomes", methods=["GET"])
# Same gate the controller's admin_required shim applies.
@permission_required(Permission.USER_MANAGE)
def dashboard():
    status = (request.args.get("status") or "").strip().lower() or None
    if status not in outcomes.FILTER_STATES:
        status = None
    try:
        page = max(1, int(request.args.get("page") or 1))
    except ValueError:
        page = 1

    emails, total, counts = outcomes.list_emails(status, page=page, per_page=PER_PAGE)
    pages = max(1, math.ceil(total / PER_PAGE))

    return render_template(
        "vim_admin_gmail_tracking.html",
        emails=emails,
        total=total,
        counts=counts,
        status=status,
        page=page,
        pages=pages,
        filter_states=outcomes.FILTER_STATES,
        labels=outcomes.LABELS,
        badge=lambda state: _BADGE.get(state, "badge-default"),
        PROCESSED=outcomes.PROCESSED,
        PENDING=outcomes.PENDING,
    )
