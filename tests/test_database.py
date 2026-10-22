import sqlite3
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path

from app.db.connection import connect, initialize
from app.db.models import Invoice, InvoiceTitle, PrintJob, ReimbursementPlan, User


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'test.db'
        initialize(self.path)
        self.db = connect(self.path)
        self.db.executemany(
            'INSERT INTO users(username, password_hash) VALUES (?, ?)',
            [('alice', 'hashed-password'), ('bob', 'hashed-password')],
        )
        self.db.executemany(
            'INSERT INTO invoice_titles(user_id, title_name) VALUES (?, ?)',
            [(1, 'Company A'), (2, 'Company B')],
        )

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def invoice(self, user_id=1, code='123', number='456', status='active', title_id=None):
        return self.db.execute(
            'INSERT INTO invoices(user_id, invoice_code, invoice_number, amount, '
            'file_path, status, title_id) VALUES (?, ?, ?, 100, ?, ?, ?)',
            (user_id, code, number, f'invoices/{user_id}/test.pdf', status, title_id),
        ).lastrowid

    def test_models_match_schema(self):
        for table, model in [('users', User), ('invoice_titles', InvoiceTitle),
                             ('invoices', Invoice), ('reimbursement_plans', ReimbursementPlan),
                             ('print_jobs', PrintJob)]:
            columns = [row['name'] for row in self.db.execute(f'PRAGMA table_info({table})')]
            self.assertEqual(columns, [field.name for field in fields(model)])

    def test_global_deduplication_including_reimbursed(self):
        self.invoice(status='reimbursed')
        for owner in (1, 2):
            with self.assertRaises(sqlite3.IntegrityError):
                self.invoice(user_id=owner)

    def test_pending_without_identifiers_and_active_validation(self):
        self.invoice(code=None, number=None, status='pending')
        self.invoice(code=None, number=None, status='pending')
        with self.assertRaises(sqlite3.IntegrityError):
            self.invoice(code=None, number=None)

    def test_cross_user_title_and_missing_owner_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.invoice(title_id=2)
        with self.assertRaises(sqlite3.IntegrityError):
            self.invoice(user_id=999)

    def test_plan_and_print_ownership(self):
        foreign_invoice = self.invoice(user_id=2)
        insert = ('INSERT INTO reimbursement_plans(user_id, title_id, target_amount, '
                  'selected_invoice_ids, total_amount) VALUES (1, ?, 100, ?, 100)')
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(insert, (2, '[]'))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(insert, (1, f'[{foreign_invoice}]'))
        for invalid in ('not-json', '{}', '["1"]'):
            with self.assertRaises((sqlite3.IntegrityError, sqlite3.OperationalError)):
                self.db.execute(insert, (1, invalid))
        own_invoice = self.invoice(number='789')
        plan = self.db.execute(insert, (1, f'[{own_invoice}]')).lastrowid
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('UPDATE reimbursement_plans SET selected_invoice_ids = ? WHERE id = ?',
                            (f'[{foreign_invoice}]', plan))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('INSERT INTO print_jobs(user_id, reimbursement_plan_id, pdf_path) '
                            'VALUES (2, ?, ?)', (plan, 'prints/2/job.pdf'))
        self.db.execute('INSERT INTO print_jobs(user_id, reimbursement_plan_id, pdf_path) '
                        'VALUES (1, ?, ?)', (plan, 'prints/1/job.pdf'))
        self.assertEqual(self.db.execute('SELECT auto_reimburse FROM print_jobs').fetchone()[0], 1)

    def test_binding_unique_and_unbind(self):
        self.db.execute("UPDATE users SET feishu_open_id = 'open_1' WHERE id = 1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE users SET feishu_open_id = 'open_1' WHERE id = 2")
        self.db.execute('UPDATE users SET feishu_open_id = NULL WHERE id = 1')
        self.db.execute("UPDATE users SET feishu_open_id = 'open_1' WHERE id = 2")

    def test_number_only_unique_across_users_and_statuses(self):
        self.invoice(code=None, number='12345678901234567890', status='reimbursed')
        for owner in (1, 2):
            with self.assertRaises(sqlite3.IntegrityError):
                self.invoice(user_id=owner, code=None, number='12345678901234567890', status='pending')
        self.invoice(code=None, number='12345678901234567891')
        # Traditional invoice numbers remain scoped to their invoice code.
        self.invoice(code='111', number='456')
        self.invoice(code='222', number='456')

    def test_v1_upgrade_preserves_records_and_plan_triggers(self):
        legacy = Path(self.temp.name) / 'legacy.db'
        with sqlite3.connect(legacy) as db:
            db.executescript((Path(__file__).parent / 'fixtures/schema_v1.sql').read_text())
            db.execute("INSERT INTO users(id,username,password_hash) VALUES (7,'old','hash')")
            db.execute("INSERT INTO invoice_titles(id,user_id,title_name) VALUES (9,7,'Buyer')")
            db.execute("INSERT INTO invoices(id,user_id,title_id,invoice_code,invoice_number,amount,file_path) VALUES (42,7,9,'123','456',100,'original.pdf')")
            db.execute("INSERT INTO reimbursement_plans(id,user_id,title_id,target_amount,selected_invoice_ids,total_amount) VALUES (3,7,9,100,'[42]',100)")
            db.execute("INSERT INTO print_jobs(user_id,reimbursement_plan_id,pdf_path) VALUES (7,3,'print.pdf')")
        initialize(legacy)
        initialize(legacy)
        with sqlite3.connect(legacy) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT id,file_path FROM invoices').fetchone(), (42,'original.pdf'))
            self.assertEqual(db.execute('SELECT selected_invoice_ids FROM reimbursement_plans').fetchone()[0], '[42]')
            self.assertEqual(db.execute('SELECT reimbursement_plan_id FROM print_jobs').fetchone()[0], 3)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            db.execute("INSERT INTO invoices(user_id,invoice_number,amount,file_path) VALUES (7,'12345678901234567890',100,'digital.pdf')")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE reimbursement_plans SET selected_invoice_ids='[999]' WHERE id=3")

    def test_failed_v1_upgrade_rolls_back_without_losing_rows(self):
        legacy = Path(self.temp.name) / 'legacy-duplicates.db'
        with sqlite3.connect(legacy) as db:
            db.executescript((Path(__file__).parent / 'fixtures/schema_v1.sql').read_text())
            db.execute("INSERT INTO users(username,password_hash) VALUES ('old','hash')")
            for _ in range(2):
                db.execute("INSERT INTO invoices(user_id,invoice_number,file_path,status) VALUES (1,'12345678901234567890','original.pdf','pending')")
        with self.assertRaises(sqlite3.IntegrityError):
            initialize(legacy)
        with sqlite3.connect(legacy) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM invoices').fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger'").fetchone()[0], 2)

    def test_initialization_is_idempotent(self):
        self.db.commit()
        initialize(self.path)
        self.assertEqual(self.db.execute('SELECT count(*) FROM users').fetchone()[0], 2)
        self.assertEqual(self.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertEqual(list(self.db.execute('PRAGMA foreign_key_check')), [])


if __name__ == '__main__':
    unittest.main()
