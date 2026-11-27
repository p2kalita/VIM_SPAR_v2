from pathlib import Path

from conftest import REAL_DB


def test_app_uses_temp_database(app):
    uri = app.config["SQLALCHEMY_DATABASE_URI"]
    assert str(REAL_DB) not in uri
    assert Path(uri.replace("sqlite:///", "")).name == "test.sqlite"


def test_existing_tables_created(ctx):
    from sqlalchemy import inspect
    from vim_database.database import db

    tables = set(inspect(db.engine).get_table_names())
    assert {"gmail_ingested_message", "invoice_document", "rejected_document"} <= tables
