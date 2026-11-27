"""
Isolated test app for vim.gmail_tracking.

Never calls app.create_app(): that function binds the real
instance/vim_database.sqlite and starts the Gmail poller. Each test gets its
own temporary SQLite file instead.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from flask import Flask

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Keep any accidental start of the background thread out of tests.
os.environ.setdefault("GMAIL_TRACKING", "0")

REAL_DB = ROOT / "instance" / "vim_database.sqlite"


def _build_app(db_file: Path, *, register_tracking: bool) -> Flask:
    from vim_database.database import configure_sqlite, db

    app = Flask(
        "vim_test",
        root_path=str(ROOT),
        template_folder=str(ROOT / "templates"),
        static_folder=str(ROOT / "static"),
    )
    app.secret_key = "test-secret"
    app.config["TESTING"] = True
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_file}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    configure_sqlite(app)
    db.init_app(app)

    with app.app_context():
        import vim_database.models  # noqa: F401
        import vim.gmail_tracking.models  # noqa: F401
        from vim.vim_controller import register_routes
        from vim.rbac import Permission, has_permission

        register_routes(app)

        @app.context_processor
        def inject_rbac():
            from flask import session

            role = session.get("role", "")
            return {
                "can": lambda perm: has_permission(role, perm),
                "Permission": Permission,
            }

        if register_tracking:
            from vim.gmail_tracking import init_gmail_tracking

            init_gmail_tracking(app)

        db.create_all()
    return app


@pytest.fixture(scope="session", autouse=True)
def _real_db_untouched():
    """Fail the run if anything wrote to the real application database."""
    before = REAL_DB.stat().st_mtime_ns if REAL_DB.exists() else None
    yield
    after = REAL_DB.stat().st_mtime_ns if REAL_DB.exists() else None
    assert before == after, "tests modified instance/vim_database.sqlite"


@pytest.fixture
def app(tmp_path):
    app = _build_app(tmp_path / "test.sqlite", register_tracking=True)
    yield app
    from vim_database.database import db

    with app.app_context():
        db.session.remove()
        db.engine.dispose()


@pytest.fixture
def ctx(app):
    """An active app context for direct model / service calls."""
    with app.app_context():
        yield app


def _client(app, role: str | None):
    client = app.test_client()
    if role:
        with client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["role"] = role
            sess["username"] = f"test-{role}"
    return client


@pytest.fixture
def admin_client(app):
    return _client(app, "admin")


@pytest.fixture
def user_client(app):
    return _client(app, "vendor")


@pytest.fixture
def anon_client(app):
    return _client(app, None)
