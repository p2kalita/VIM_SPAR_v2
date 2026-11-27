import subprocess
import sys

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from conftest import ROOT


def _columns(table):
    from vim_database.database import db

    return {c["name"] for c in inspect(db.engine).get_columns(table)}


def test_new_tables_created_with_expected_columns(ctx):
    assert _columns("gmail_ingested_attachment") == {
        "AttachmentID", "IngestID", "OriginalName", "StoredFileName",
        "StoragePath", "CapturedStatus", "CapturedDetail",
        "CapturedInvoiceID", "LinkMethod", "CreatedDate",
    }
    assert _columns("gmail_ingest_reconcile") == {
        "IngestID", "State", "Method", "Attempts", "LastCheckedAt",
    }


def test_duplicate_attachment_rejected(ctx):
    from vim_database.database import db
    from vim_database.models import GmailIngestedMessage
    from vim.gmail_tracking.models import GmailIngestedAttachment

    msg = GmailIngestedMessage(GmailMessageId="m1", Status="ingested")
    db.session.add(msg)
    db.session.commit()

    for _ in range(2):
        db.session.add(GmailIngestedAttachment(
            IngestID=msg.IngestID, OriginalName="a.pdf", StoredFileName="u_a.pdf",
        ))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


_COLUMNS_SCRIPT = r"""
import sys, json, tempfile, os
sys.path.insert(0, {root!r})
from flask import Flask
from sqlalchemy import inspect
from vim_database.database import db, configure_sqlite
app = Flask("x")
path = os.path.join(tempfile.mkdtemp(), "c.sqlite")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + path
configure_sqlite(app)
db.init_app(app)
with app.app_context():
    import vim_database.models
    if {with_tracking}:
        import vim.gmail_tracking.models
    db.create_all()
    cols = [c["name"] for c in inspect(db.engine).get_columns("gmail_ingested_message")]
print(json.dumps(cols))
"""


def _message_columns(with_tracking: bool) -> list:
    import json

    out = subprocess.run(
        [sys.executable, "-c", _COLUMNS_SCRIPT.format(root=str(ROOT), with_tracking=with_tracking)],
        capture_output=True, text=True, check=True, cwd=str(ROOT),
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_existing_message_table_not_altered():
    assert _message_columns(False) == _message_columns(True)
