"""
vim/rbac.py
===========
Role-Based Access Control (RBAC) for the Vendor Invoice Management system.

Concepts
--------
* Permission  — a named capability string  e.g. "invoice.approve"
* Role        — a job title               e.g. "admin", "approver", "vendor"
* ROLE_PERMISSIONS — maps every role to the set of permissions it holds

How to use
----------
1. Protect a route with a permission:

    from vim.rbac import permission_required, Permission

    @app.route('/admin/users')
    @permission_required(Permission.USER_MANAGE)
    def admin_users():
        ...

2. Guard elements in Jinja templates (after context_processor is registered
   in app.py):

    {% if can(Permission.INVOICE_APPROVE) %}
      <button>Approve</button>
    {% endif %}

3. Check a permission programmatically:

    from vim.rbac import has_permission
    if has_permission(session.get('role'), Permission.INVOICE_DELETE):
        ...
"""

from functools import wraps
from flask import session, flash, redirect, url_for
from vim_logger import get_logger

logger = get_logger("vim.rbac")


# ---------------------------------------------------------------------------
# Permission constants
# Using dot-separated strings keeps them human-readable and easy to extend.
# ---------------------------------------------------------------------------
class Permission:
    # Invoice operations
    INVOICE_UPLOAD   = "invoice.upload"    # upload a new invoice document
    INVOICE_VIEW_ALL = "invoice.view_all"  # see every invoice in the system
    INVOICE_VIEW_OWN = "invoice.view_own"  # see only own vendor's invoices
    INVOICE_APPROVE  = "invoice.approve"   # approve / reject an invoice
    INVOICE_DELETE   = "invoice.delete"    # permanently delete an invoice

    # User & vendor management
    USER_MANAGE      = "user.manage"       # create / edit / delete users
    VENDOR_MANAGE    = "vendor.manage"     # create / edit / delete vendors

    # System
    SETTINGS_EDIT    = "settings.edit"     # edit system configuration
    PIPELINE_VIEW    = "pipeline.view"     # view processing pipeline monitor

    # Analytics & AI
    REPORT_VIEW      = "report.view"       # view KPI / analytics dashboards
    RAG_QUERY        = "rag.query"         # use the AI chatbot / RAG assistant


# ---------------------------------------------------------------------------
# Role -> Permission mapping
# To add a new role  : add a key here and list which permissions it has.
# To add a new perm  : add the constant above and list it for each role.
# ---------------------------------------------------------------------------
ROLE_PERMISSIONS: dict = {

    "admin": {
        Permission.INVOICE_UPLOAD,
        Permission.INVOICE_VIEW_ALL,
        Permission.INVOICE_VIEW_OWN,
        Permission.INVOICE_APPROVE,
        Permission.INVOICE_DELETE,
        Permission.USER_MANAGE,
        Permission.VENDOR_MANAGE,
        Permission.SETTINGS_EDIT,
        Permission.PIPELINE_VIEW,
        Permission.REPORT_VIEW,
        Permission.RAG_QUERY,
    },

    "approver": {
        Permission.INVOICE_VIEW_ALL,
        Permission.INVOICE_VIEW_OWN,
        Permission.INVOICE_APPROVE,
        Permission.REPORT_VIEW,
    },

    "vendor": {
        Permission.INVOICE_UPLOAD,
        Permission.INVOICE_VIEW_OWN,
    },
}


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------
def has_permission(role: str, permission: str) -> bool:
    """Return True if *role* has *permission*, False otherwise.

    Args:
        role:       The user's role string, e.g. ``"admin"``.
        permission: A :class:`Permission` constant, e.g.
                    ``Permission.INVOICE_APPROVE``.
    """
    return permission in ROLE_PERMISSIONS.get(role or "", set())


def get_permissions(role: str) -> frozenset:
    """Return the full set of permissions for a given role."""
    return frozenset(ROLE_PERMISSIONS.get(role or "", set()))


# ---------------------------------------------------------------------------
# Route decorator
# ---------------------------------------------------------------------------
def permission_required(permission: str):
    """Flask route decorator that enforces a single permission gate.

    Redirects to login if the user is not authenticated, and to the user's
    home page with a flash message if they lack the required permission.

    Usage::

        @app.route('/admin/users')
        @permission_required(Permission.USER_MANAGE)
        def admin_users():
            ...
    """
    def decorator(view_func):
        @wraps(view_func)
        def wrapped(*args, **kwargs):
            # 1. Authentication check
            if "user_id" not in session:
                flash("Please log in to continue.", "warning")
                return redirect(url_for("login"))

            role = session.get("role", "")

            # 2. Permission check
            if not has_permission(role, permission):
                import flask as _flask
                logger.warning(
                    "[RBAC] DENIED  user_id=%s role=%s permission=%s path=%s",
                    session.get("user_id"), role, permission,
                    _flask.request.path,
                )
                flash("You don't have permission to access that page.", "danger")
                return _role_home(role)

            logger.debug(
                "[RBAC] ALLOWED user_id=%s role=%s permission=%s",
                session.get("user_id"), role, permission,
            )
            return view_func(*args, **kwargs)

        return wrapped
    return decorator


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _role_home(role: str):
    """Redirect a user to their natural landing page after an access denial."""
    if role == "admin":
        return redirect(url_for("admin_activities"))
    elif role == "approver":
        return redirect(url_for("approver_approvals"))
    else:
        user_id = session.get("user_id", 0)
        return redirect(url_for("user_home", user_id=user_id))
