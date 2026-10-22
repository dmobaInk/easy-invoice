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
    finally:
        connection.close()
