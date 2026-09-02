import sqlite3
import os
from vim_logger import get_logger

logger = get_logger("vim.check_db")

BASEDIR = os.path.abspath(os.path.dirname(__file__))
db_path = os.path.join(BASEDIR, "instance", "vim_database.sqlite")

logger.info("[CHECK-DB] Looking for DB at: %s", os.path.abspath(db_path))
logger.info("[CHECK-DB] File exists: %s", os.path.exists(db_path))

if os.path.exists(db_path):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = cur.fetchall()
    logger.info("[CHECK-DB] Tables (%d): %s", len(tables), tables)

    try:
        cur.execute("SELECT * FROM user;")
        users = cur.fetchall()
        logger.info("[CHECK-DB] Users (%d): %s", len(users), users)
    except Exception as e:
        logger.warning("[CHECK-DB] Could not query user table: %s", e)

    conn.close()
else:
    logger.warning("[CHECK-DB] Database file not found at %s", db_path)