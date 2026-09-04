from flask import Flask
from vim_database.database import db, configure_sqlite, apply_sqlite_pragmas
import os
from pathlib import Path
from dotenv import load_dotenv
from vim.extraction import config as extraction_config

load_dotenv(
    Path(__file__).resolve().parent / ".env",
    override=True
)

from vim_logger import get_logger
logger = get_logger("vim.app")

app = None

def create_app():

    logger.info("=" * 60)
    logger.info("VIM Application starting up ...")
    logger.info("=" * 60)

    app = Flask(__name__)

    # Auto-generate a secret key on first run and save it so sessions
    # survive server restarts without any manual configuration.
    _key_file = Path(__file__).resolve().parent / "instance" / "secret.key"
    _key_file.parent.mkdir(parents=True, exist_ok=True)
    if _key_file.exists():
        app.secret_key = _key_file.read_bytes()
    else:
        import secrets as _secrets
        _key = _secrets.token_bytes(32)
        _key_file.write_bytes(_key)
        app.secret_key = _key
        logger.info("Generated new secret key -> instance/secret.key")
    logger.info("SECRET_KEY loaded OK")

    app.debug = True
    # Anchor the database to this file's directory. A relative sqlite:/// URI
    # is resolved against Flask's instance_path, which falls back to the
    # working directory and silently pointed the app at an empty database
    # whenever the server was started from a different folder.
    db_path = Path(__file__).resolve().parent / "instance" / "vim_database.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"

    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    configure_sqlite(app)
    db.init_app(app)
    logger.info("SQLAlchemy initialised — DB: %s", app.config["SQLALCHEMY_DATABASE_URI"])

    with app.app_context():

        apply_sqlite_pragmas()

        import vim_database.models

        from vim.vim_controller import register_routes

        register_routes(app)
        logger.info("Routes registered")

        try:
            db.create_all()
            logger.info("db.create_all() succeeded")
        except Exception as e:
            logger.error("db.create_all() FAILED: %s", e)

        # create_all() does not alter tables that already exist, so columns
        # added to the models later have to be applied to the existing file.
        try:
            from vim_database.migrate import sync_columns
 
            sync_columns()
        except Exception as e:
            logger.error("schema migration FAILED: %s", e)

        # ---------------------------------------------
        # LOAD API KEYS
        # ---------------------------------------------

        llama, groq = extraction_config.load_keys_into_app(app)

        if llama and groq:
            logger.info("API keys loaded from %s", extraction_config.ENV_PATH)
        else:
            logger.warning("API keys MISSING — check %s", extraction_config.ENV_PATH)
            
    return app

app = create_app()

if __name__ == "__main__":

    logger.info("Starting Flask dev server on 127.0.0.1:5002 (debug=True)")
    app.run(
        debug=True,
        use_reloader=False,
        threaded=True,
        host="127.0.0.1",
        port=5002
    )