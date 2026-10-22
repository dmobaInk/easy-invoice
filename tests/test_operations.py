from contextlib import closing
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.config import Settings
from app.db.connection import connect, initialize
from scripts.backup import BackupError, create_backup
from scripts.check_config import check
from scripts.restore import restore_backup


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.data = self.root / 'source-data'
        self.database = self.data / 'app.db'
        initialize(self.database)
        self.original = self.data / 'invoices' / '1' / 'invoice.pdf'
        self.printed = self.data / 'prints' / '1' / 'print.pdf'
        for path in (self.original, self.printed):
            path.parent.mkdir(parents=True)
            path.write_bytes(b'fixture-content-for-backup')
        (self.data / 'session.key').write_text('a' * 64)
        events = self.data / 'feishu-events'
        events.mkdir()
        (events / 'event.json').write_text('{"test":true}')
        (events / 'event.result').write_text('{"delivered":true}')
        (self.data / '.env').write_text('SECRET=must-not-be-copied')
        with closing(connect(self.database)) as db, db:
            db.execute("INSERT INTO users(username,password_hash) VALUES ('alice','hashed')")
            db.execute("INSERT INTO invoice_titles(user_id,title_name) VALUES (1,'Company')")
            db.execute('INSERT INTO invoices(user_id,title_id,invoice_code,invoice_number,amount,file_path) '
                       "VALUES (1,1,'012345678901','12345678',100,?)", (str(self.original),))
            db.execute('INSERT INTO reimbursement_plans(user_id,title_id,target_amount,selected_invoice_ids,total_amount) '
                       "VALUES (1,1,100,'[1]',100)")
            db.execute('INSERT INTO print_jobs(user_id,reimbursement_plan_id,pdf_path) VALUES (1,1,?)', (str(self.printed),))
        self.snapshot = self.root / 'snapshots' / 'one'

    def tearDown(self):
        self.temp.cleanup()

    def backup(self):
        return create_backup(self.database, self.snapshot, service_stopped=True)

    def test_complete_backup_restore_rewrites_paths_and_preserves_private_files(self):
        self.backup()
        self.assertFalse(any(path.name == '.env' for path in self.snapshot.rglob('*')))
        restored = self.root / 'new-location'
        restored_db = restore_backup(self.snapshot, restored, service_stopped=True)
        with closing(connect(restored_db)) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(db.execute('SELECT file_path FROM invoices').fetchone()[0], str(restored / 'invoices/1/invoice.pdf'))
            self.assertEqual(db.execute('SELECT pdf_path FROM print_jobs').fetchone()[0], str(restored / 'prints/1/print.pdf'))
        self.assertEqual((restored / 'session.key').read_text(), 'a' * 64)
        self.assertTrue((restored / 'feishu-events/event.result').exists())
        self.assertEqual((restored / 'session.key').stat().st_mode & 0o777, 0o600)

    def test_wal_commits_are_included_in_database_snapshot(self):
        with closing(connect(self.database)) as source:
            with source:
                source.execute("UPDATE users SET name = 'in-wal' WHERE id = 1")
            self.backup()
        with closing(connect(self.snapshot / 'database.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT name FROM users').fetchone()[0], 'in-wal')

    def test_stop_acknowledgement_and_existing_targets(self):
        with self.assertRaises(BackupError):
            create_backup(self.database, self.snapshot)
        self.backup()
        with self.assertRaises(BackupError):
            self.backup()
        target = self.root / 'existing'
        target.mkdir()
        (target / 'keep.txt').write_text('keep')
        with self.assertRaises(BackupError):
            restore_backup(self.snapshot, target, service_stopped=True)
        self.assertEqual((target / 'keep.txt').read_text(), 'keep')
        with self.assertRaises(BackupError):
            restore_backup(self.snapshot, self.root / 'not-created')

    def test_missing_file_and_nested_snapshot_are_rejected(self):
        with self.assertRaises(BackupError):
            create_backup(self.database, self.data / 'backup', service_stopped=True)
        self.original.unlink()
        with self.assertRaises(BackupError):
            self.backup()
        self.assertFalse(self.snapshot.exists())

    def test_modified_snapshot_is_rejected_before_creating_target(self):
        self.backup()
        (self.snapshot / 'files/session.key').write_text('tampered')
        output = self.root / 'output'
        with self.assertRaises(BackupError):
            restore_backup(self.snapshot, output, service_stopped=True)
        self.assertFalse(output.exists())

    def test_symlinks_and_manifest_path_traversal_are_rejected(self):
        (self.data / 'invoices/1/link.pdf').symlink_to(self.original)
        with self.assertRaises(BackupError):
            self.backup()
        (self.data / 'invoices/1/link.pdf').unlink()
        self.backup()
        path = self.snapshot / 'manifest.json'
        manifest = json.loads(path.read_text())
        manifest['files']['files/invoices/../../../escape'] = {'sha256': 'x', 'size': 1}
        path.write_text(json.dumps(manifest))
        with self.assertRaises(BackupError):
            restore_backup(self.snapshot, self.root / 'escape-output', service_stopped=True)
        self.assertFalse((self.root / 'escape-output').exists())


class ConfigChecks(unittest.TestCase):
    def test_preflight_does_not_create_database_or_administrator(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(Path(directory) / 'data/app.db',
                                bootstrap_admin_username='owner', bootstrap_admin_password='never-print-me-123')
            messages = check(settings)
            self.assertFalse(settings.database_path.parent.exists())
            self.assertNotIn('never-print-me-123', '\n'.join(messages))

    def test_incomplete_admin_or_cloud_credentials_fail(self):
        settings = Settings(Path('/unused/app.db'))
        with self.assertRaises(ValueError):
            check(replace(settings, bootstrap_admin_username='owner'))
        with self.assertRaises(ValueError):
            check(replace(settings, aliyun_access_key_id='id-only'))

    def test_boolean_config_and_example(self):
        with patch.dict(os.environ, {'COOKIE_SECURE': 'typo'}, clear=True):
            with self.assertRaisesRegex(ValueError, 'COOKIE_SECURE'):
                Settings.from_env(None)
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env(Path('.env.example'))
        self.assertFalse(settings.feishu_enabled)
        self.assertFalse(settings.cookie_secure)
        self.assertEqual(settings.feishu_app_secret, '')
