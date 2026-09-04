# Imports the SQLAlchemy class from the flask_sqlalchemy library. This class is the main entry point for using the extension.
import threading
import time

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

# Creates an instance of the SQLAlchemy class. This object, conventionally named 'db',
# represents the vim_database and provides access to all the functions and classes from SQLAlchemy,
# like the Model class for defining vim_database tables.
db = SQLAlchemy()

# SQLite allows one writer. Upload workers, vendor-decide, and validation
# all commit; this lock plus WAL/busy_timeout stops "database is locked".
write_lock = threading.RLock()


def configure_sqlite(app):
    """Engine options must be set before db.init_app()."""
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "poolclass": NullPool,
        "connect_args": {
            "timeout": 30,
            "check_same_thread": False,
        },
    }


def apply_sqlite_pragmas():
    """WAL lets readers proceed while a writer finishes; busy_timeout waits."""

    @event.listens_for(db.engine, "connect")
    def _on_connect(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    with db.engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))
        conn.execute(text("PRAGMA busy_timeout=30000"))
        conn.commit()


def is_locked_error(exc) -> bool:
    msg = str(exc).lower()
    return "database is locked" in msg or "database is busy" in msg


def commit():
    """Commit, waiting and retrying if SQLite still has another writer."""
    attempts = 8
    for attempt in range(attempts):
        try:
            with write_lock:
                db.session.commit()
            return
        except OperationalError as e:
            db.session.rollback()
            if not is_locked_error(e) or attempt >= attempts - 1:
                raise
            time.sleep(min(0.25 * (2 ** attempt), 2.0))
