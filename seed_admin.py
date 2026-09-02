"""Create a default admin user and vendor for local development."""

import argparse

from werkzeug.security import generate_password_hash
from vim_logger import get_logger

from app import create_app
from vim_database.database import db
from vim_database.models import User, Vendor

logger = get_logger("vim.seed")

def seed(fresh: bool = False):
    app = create_app()
    with app.app_context():
        if fresh:
            db.drop_all()
            db.create_all()
            logger.info("[SEED] Database recreated with current schema.")

        vendor = Vendor.query.filter_by(VendorName="Default Vendor").first()
        if not vendor:
            vendor = Vendor(
                VendorName="Default Vendor",
                GSTNumber="",
                Email="vendor@example.com",
                Status=1,
            )
            db.session.add(vendor)
            db.session.flush()
            logger.info("[SEED] Created default vendor: %s", vendor.VendorName)

        admin = User.query.filter_by(Email="admin@vim.local").first()
        if not admin:
            admin = User(
                Username="admin",
                PasswordHash=generate_password_hash("admin123"),
                Email="admin@vim.local",
                Role="admin",
                VendorID=vendor.VendorID,
                IsActive=True,
            )
            db.session.add(admin)
            logger.info("[SEED] Created default admin user: %s", admin.Username)

        db.session.commit()
        logger.info("[SEED] Seed complete — login: admin@vim.local / admin123")
        print("Seed complete.")
        print("  Login: admin@vim.local / admin123")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed VIM admin user")
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Drop and recreate all tables (use when schema changed)",
    )
    args = parser.parse_args()
    seed(fresh=args.fresh)
