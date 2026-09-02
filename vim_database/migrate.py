"""Additive schema migration for the SQLite database.

db.create_all() creates missing tables but never alters existing ones, so a
column added to a model after the database file was created stays missing and
every query touching it fails with "no such column". This adds those columns.

Only nullable columns are added, and nothing is ever dropped, renamed, or
retyped, so running it against an up-to-date database is a no-op.
"""

from sqlalchemy import inspect, text
from vim_logger import get_logger

from vim_database.database import db

logger = get_logger("vim.database.migrate")


def sync_columns(verbose: bool = True) -> tuple[list[str], list[str]]:
    """
    Add model columns that are missing from existing tables.

    Returns (added, skipped) as "table.column" strings. Skipped columns are
    NOT NULL ones, which cannot be added to a table that already has rows
    without a value to backfill; those need a hand-written migration.
    """
    inspector = inspect(db.engine)
    existing_tables = set(inspector.get_table_names())
    dialect = db.engine.dialect

    added: list[str] = []
    skipped: list[str] = []

    for table_name, table in db.metadata.tables.items():
        if table_name not in existing_tables:
            continue

        present = {col["name"] for col in inspector.get_columns(table_name)}

        for column in table.columns:
            if column.name in present:
                continue

            label = f"{table_name}.{column.name}"

            if column.primary_key or not column.nullable:
                skipped.append(label)
                continue

            column_type = column.type.compile(dialect=dialect)
            db.session.execute(
                text(
                    f'ALTER TABLE "{table_name}" '
                    f'ADD COLUMN "{column.name}" {column_type}'
                )
            )
            added.append(label)

    if added:
        db.session.commit()

    if added:
        logger.info("[MIGRATE] Schema migration added %d column(s): %s", len(added), ", ".join(added))
    if skipped:
        logger.warning(
            "[MIGRATE] Schema migration skipped %d NOT NULL column(s) (needs a manual backfill): %s",
            len(skipped), ", ".join(skipped)
        )

    return added, skipped
