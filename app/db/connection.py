"""SQLite connections. All callers must use this factory to enforce foreign keys."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parents[2] / 'sql' / '001_init.sql'


def connect(path: str | Path) -> sqlite3.Connection:
    # FastAPI may run dependency setup, endpoint and cleanup on different workers.
    # Each request owns its connection; callers must not use one concurrently.
    connection = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys = ON')
    connection.execute('PRAGMA busy_timeout = 30000')
    return connection


def initialize(path: str | Path) -> None:
    """Create a new database or reapply the idempotent initial schema."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = connect(path)
    try:
        connection.execute('PRAGMA journal_mode = WAL')
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 1, 2):
            raise RuntimeError(f'Unsupported database schema version: {version}')
        schema = SCHEMA_PATH.read_text(encoding='utf-8')
        if version == 1:
            migration = (SCHEMA_PATH.parent / '002_support_digital_invoices.sql').read_text(encoding='utf-8')
            connection.execute('PRAGMA foreign_keys = OFF')
            try:
                # Rebuild the table and recreate plan triggers in one transaction.
                schema_body = schema.replace('PRAGMA foreign_keys = ON;', '').replace('BEGIN IMMEDIATE;', '')
                connection.executescript('BEGIN IMMEDIATE;\n' + migration + schema_body.replace('COMMIT;', ''))
                if connection.execute('PRAGMA foreign_key_check').fetchone() is not None:
                    raise RuntimeError('Database migration failed foreign key validation')
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.execute('PRAGMA foreign_keys = ON')
        else:
            connection.executescript(schema)
        connection.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS user_invoice_tags (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                PRIMARY KEY(user_id, name)
            );
            CREATE TABLE IF NOT EXISTS invoice_tag_assignments (
                invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                PRIMARY KEY(invoice_id, name)
            );
            CREATE TABLE IF NOT EXISTS reimbursement_dates (
                invoice_id INTEGER PRIMARY KEY REFERENCES invoices(id) ON DELETE CASCADE,
                reimbursed_at TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS invoice_reimbursed AFTER UPDATE OF status ON invoices
            WHEN NEW.status = 'reimbursed' AND OLD.status != NEW.status
            BEGIN
                INSERT OR REPLACE INTO reimbursement_dates VALUES
                    (NEW.id, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
            END;
            CREATE TRIGGER IF NOT EXISTS invoice_reopened AFTER UPDATE OF status ON invoices
            WHEN OLD.status = 'reimbursed' AND NEW.status != OLD.status
            BEGIN
                DELETE FROM reimbursement_dates WHERE invoice_id = NEW.id;
            END;
            INSERT OR IGNORE INTO reimbursement_dates
            SELECT i.id, max(j.created_at) FROM print_jobs j
            JOIN reimbursement_plans p ON p.id = j.reimbursement_plan_id
            JOIN json_each(p.selected_invoice_ids) entry
            JOIN invoices i ON i.id = entry.value AND i.user_id = j.user_id
            WHERE j.auto_reimburse = 1 AND i.status = 'reimbursed'
              AND substr(j.created_at, 1, 19) = substr(i.updated_at, 1, 19)
            GROUP BY i.id;
            COMMIT;
        """)
        connection.execute("""CREATE TABLE IF NOT EXISTS invoice_remarks (
            invoice_id INTEGER PRIMARY KEY REFERENCES invoices(id) ON DELETE CASCADE,
            remarks TEXT NOT NULL
        )""")
        connection.execute("""CREATE TABLE IF NOT EXISTS invoice_item_names (
    invoice_id INTEGER PRIMARY KEY REFERENCES invoices(id) ON DELETE CASCADE,
    names TEXT NOT NULL
);""")
    finally:
        connection.close()
