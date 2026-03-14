"""
Migration: Add patrol_fail_count column to products table.
Run once against the production database.
"""
import os
import sqlite3


def migrate():
    db_path = os.environ.get("DATABASE_URL", "mercari.db")
    # Strip sqlite:/// prefix if present
    if db_path.startswith("sqlite:///"):
        db_path = db_path[len("sqlite:///"):]

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Check if column already exists
    cursor.execute("PRAGMA table_info(products)")
    columns = [row[1] for row in cursor.fetchall()]

    if "patrol_fail_count" not in columns:
        cursor.execute(
            "ALTER TABLE products ADD COLUMN patrol_fail_count INTEGER DEFAULT 0"
        )
        conn.commit()
        print("Added patrol_fail_count column to products table.")
    else:
        print("patrol_fail_count column already exists, skipping.")

    conn.close()


if __name__ == "__main__":
    migrate()
