"""Short SQLite transactions enforce message idempotency and global deduplication."""

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.db.connection import connect
from app.db.models import Invoice
from app.integrations.aliyun.invoice_ocr import RecognizedInvoice


@dataclass(frozen=True)
class SaveResult:
    code: str
    invoice: Optional[Invoice] = None


class InvoiceRepository:
    def __init__(self, database_path: Path):
        self.database_path = database_path

    @staticmethod
    def _existing_message(db, user_id, message_id):
        row = db.execute('SELECT * FROM invoices WHERE feishu_message_id = ?', (message_id,)).fetchone()
        if row is None:
            return None
        if row['user_id'] != user_id:
            return SaveResult('message_owner_changed')
        return SaveResult('already_processed', Invoice(**dict(row)))

    def find_message(self, user_id: int, message_id: str) -> Optional[SaveResult]:
        with closing(connect(self.database_path)) as db:
            return self._existing_message(db, user_id, message_id)

    def save(self, user_id: int, open_id: str, message_id: str, path: Path,
             data: RecognizedInvoice, error: Optional[str] = None) -> SaveResult:
        return self._save(user_id, path, data, error, open_id=open_id, message_id=message_id)

    def save_upload(self, user_id: int, path: Path, data: RecognizedInvoice,
                    error: Optional[str] = None) -> SaveResult:
        return self._save(user_id, path, data, error)

    def _save(self, user_id, path, data, error, *, open_id=None, message_id=None):
        with closing(connect(self.database_path)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            # Web uploads require an active account, not a Feishu binding.
            query = 'SELECT id FROM users WHERE id = ? AND is_active = 1'
            parameters = [user_id]
            if open_id is not None:
                query += ' AND feishu_open_id = ?'
                parameters.append(open_id)
            user = db.execute(query, parameters).fetchone()
            if user is None:
                return SaveResult('binding_changed' if open_id is not None else 'account_disabled')
            existing = self._existing_message(db, user_id, message_id) if message_id is not None else None
            if existing is not None:
                return existing
            if data.invoice_number is not None:
                row = db.execute('SELECT * FROM invoices WHERE invoice_code IS ? AND invoice_number = ?',
                                 (data.invoice_code, data.invoice_number)).fetchone()
                if row is not None:
                    if row['user_id'] == user_id:
                        return SaveResult('duplicate_same_user', Invoice(**dict(row)))
                    # Never expose the other user's record to a response serializer.
                    return SaveResult('duplicate_other_user')
            title_id = None
            if data.buyer_name:
                db.execute('INSERT INTO invoice_titles(user_id, title_name, tax_number) VALUES (?, ?, ?) '
                           'ON CONFLICT(user_id, title_name) DO NOTHING',
                           (user_id, data.buyer_name, data.buyer_tax_number))
                title_id = db.execute('SELECT id FROM invoice_titles WHERE user_id = ? AND title_name = ?',
                                      (user_id, data.buyer_name)).fetchone()['id']
            failure = error or data.validation_error
            status = 'pending' if failure else 'active'
            cursor = db.execute(
                'INSERT INTO invoices(user_id, title_id, invoice_code, invoice_number, invoice_date, '
                'buyer_name, seller_name, invoice_type, amount, status, file_path, feishu_message_id, ocr_error) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (user_id, title_id, data.invoice_code, data.invoice_number, data.invoice_date,
                 data.buyer_name, data.seller_name, data.invoice_type,
                 float(data.amount) if data.amount is not None else None, status, str(path), message_id, failure),
            )
            if data.remarks:
                db.execute('INSERT INTO invoice_remarks(invoice_id, remarks) VALUES (?, ?)',
                           (cursor.lastrowid, data.remarks))
            if data.item_names:
                db.execute('INSERT INTO invoice_item_names(invoice_id, names) VALUES (?, ?)',
                           (cursor.lastrowid, ','.join(data.item_names)))
            invoice = db.execute('SELECT * FROM invoices WHERE id = ?', (cursor.lastrowid,)).fetchone()
            return SaveResult('created', Invoice(**dict(invoice)))
