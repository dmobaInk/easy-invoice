from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
import itertools
import json
import logging
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import pymupdf

from app.config import Settings
from app.db.connection import connect, initialize
from app.main import app
from app.security.auth import bootstrap_admin, hash_password, verify_password
from app.services.pdf_printing import PdfGenerationError, generate_a4
from app.services.reimbursement import OptimizationLimitError, optimize
from app.services.storage import LocalStorage

PASSWORD = 'Test-password-123'
PASSWORD_HASH = hash_password(PASSWORD)


def sample_pdf(pages=1):
    with pymupdf.open() as document:
        for index in range(pages):
            page = document.new_page(width=600, height=280)
            page.draw_rect(pymupdf.Rect(20, 20, 580, 260), color=(.2, .4, .3))
            page.insert_text((40, 70), f'INVOICE FIRST PAGE' if index == 0 else 'DO NOT PRINT SECOND PAGE', fontsize=20)
            page.insert_text((40, 120), 'Code: 012345678901   Number: 12345678', fontsize=14)
            page.insert_text((40, 180), 'Total: CNY 100.00', fontsize=18)
        return document.tobytes()


class WebApiTests(unittest.TestCase):
    def setUp(self):
        from app.api.auth import _attempts
        _attempts.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.settings = Settings(self.root / 'app.db', feishu_app_secret='sample-secret-for-redaction')
        initialize(self.settings.database_path)
        with self.db() as db, db:
            db.executemany('INSERT INTO users(username, password_hash, role) VALUES (?, ?, ?)',
                           [('alice', PASSWORD_HASH, 'user'), ('bob', PASSWORD_HASH, 'user'), ('admin', PASSWORD_HASH, 'admin')])
        self.patch = patch('app.main.Settings.from_env', return_value=self.settings)
        self.patch.start()
        self.client = TestClient(app)
        self.client.__enter__()
        self.csrf = self.client.get('/api/auth/session').json()['csrf_token']

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.patch.stop()
        self.temp.cleanup()

    def db(self):
        return closing(connect(self.settings.database_path))

    def mutate(self, method, path, **kwargs):
        return self.client.request(method, path, headers={'X-CSRF-Token': self.csrf}, **kwargs)

    def login(self, username='alice'):
        response = self.mutate('POST', '/api/auth/login', json={'username': username, 'password': PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        self.csrf = response.json()['csrf_token']

    def title(self, user_id=1, name='Test Company'):
        with self.db() as db, db:
            return db.execute('INSERT INTO invoice_titles(user_id, title_name) VALUES (?, ?)', (user_id, name)).lastrowid

    def invoice(self, user_id=1, title_id=None, amount=100, status='active', number=None):
        number = number or str(random.randrange(10000000, 99999999))
        path = LocalStorage(self.root / 'invoices').save_invoice(user_id, 'test_' + number, sample_pdf(pages=2))
        with self.db() as db, db:
            return db.execute('INSERT INTO invoices(user_id, title_id, invoice_code, invoice_number, invoice_date, buyer_name, '
                              'seller_name, invoice_type, amount, status, file_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                              (user_id, title_id, '012345678901', number, '2026-10-21', 'Test Company', 'Seller',
                               '普通发票', amount, status, str(path))).lastrowid

    def plan(self, title_id, amount=100, allow_exceed=False):
        response = self.mutate('POST', '/api/reimbursements/optimize',
                               json={'title_id': title_id, 'target_amount': amount, 'allow_exceed': allow_exceed})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_upload_recognizes_reuses_titles_and_rejects_global_duplicate(self):
        from app.integrations.aliyun.invoice_ocr import RecognizedInvoice
        recognized = RecognizedInvoice('012345678901', '12345678', '2026-10-21',
                                      '自动识别公司', 'Seller', '普通发票', Decimal('100'), 'TAX123')
        self.login()
        def upload():
            return self.mutate('POST', '/api/invoices/upload', files={'file': ('invoice.pdf', sample_pdf(), 'application/pdf')})
        with patch('app.api.invoices.AliyunInvoiceOcr') as ocr:
            ocr.return_value.recognize.return_value = recognized
            first = upload()
            self.assertEqual(first.status_code, 201, first.text)
            self.assertEqual(first.json()['status'], 'active')
            title_id = first.json()['title_id']
            ocr.return_value.recognize.return_value = replace(recognized, invoice_number='12345679')
            second = upload()
            self.assertEqual(second.json()['title_id'], title_id)
            self.assertEqual(upload().status_code, 409)
            self.login('bob')
            self.assertEqual(upload().status_code, 409)
            ocr.return_value.recognize.return_value = replace(recognized, invoice_number='12345680')
            third = upload()
            self.assertEqual(third.status_code, 201, third.text)
            self.assertNotEqual(third.json()['title_id'], title_id)
        with self.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM invoices').fetchone()[0], 3)
            self.assertEqual(db.execute('SELECT count(*) FROM invoice_titles').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT tax_number FROM invoice_titles WHERE id=?', (title_id,)).fetchone()[0], 'TAX123')
        self.assertEqual(len(list((self.root / 'invoices').rglob('*.pdf'))), 3)

    def test_upload_ocr_failure_preserves_original(self):
        from app.integrations.aliyun.invoice_ocr import OcrError
        self.login()
        with patch('app.api.invoices.AliyunInvoiceOcr') as ocr:
            ocr.return_value.recognize.side_effect = OcrError('ocr_unavailable')
            response = self.mutate('POST', '/api/invoices/upload', files={'file': ('invoice.pdf', sample_pdf(), 'application/pdf')})
            self.assertEqual(response.status_code, 201, response.text)
            self.assertEqual(response.json()['status'], 'pending')
        self.assertEqual(len(list((self.root / 'invoices').rglob('*.pdf'))), 1)

    def test_digital_upload_edit_reimbursement_and_global_dedup(self):
        from app.integrations.aliyun.invoice_ocr import parse_invoice_data
        data = parse_invoice_data(json.dumps({'data': {
            'invoiceCode': '', 'invoiceNumber': '12345678901234567890',
            'invoiceDate': '2026-10-22', 'purchaserName': 'Digital Buyer',
            'sellerName': 'Seller', 'invoiceType': '数电普通发票', 'totalAmount': '100.00'}}))
        self.login()
        with patch('app.api.invoices.AliyunInvoiceOcr') as ocr:
            ocr.return_value.recognize.return_value = data
            def upload():
                return self.mutate('POST', '/api/invoices/upload', files={'file': ('digital.pdf', sample_pdf(), 'application/pdf')})
            response = upload()
            self.assertEqual(response.status_code, 201, response.text)
            row = response.json()
            self.assertIsNone(row['invoice_code'])
            self.assertEqual(row['status'], 'active')
            endpoint = f"/api/invoices/{row['id']}"
            self.assertEqual(self.mutate('PUT', endpoint, json={'invoice_code': None, 'status':'reimbursed'}).status_code, 200)
            self.assertEqual(upload().status_code, 409)
            self.mutate('PUT', endpoint, json={'status':'active'})
            self.assertEqual(self.plan(row['title_id'])['total_amount'], 100)
            other = self.invoice(status='pending')
            duplicate_edit = self.mutate('PUT', f'/api/invoices/{other}', json={
                'invoice_code':None, 'invoice_number':data.invoice_number, 'status':'pending'})
            self.assertEqual(duplicate_edit.status_code, 409)
            self.login('bob')
            self.assertEqual(upload().status_code, 409)

    def test_preview_pages_permissions_and_original_download(self):
        invoice_id = self.invoice()
        endpoint = f'/api/invoices/{invoice_id}'
        self.assertEqual(self.client.get(endpoint + '/preview').status_code, 401)
        self.login()
        first = self.client.get(endpoint + '/preview')
        second = self.client.get(endpoint + '/preview?page=2')
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.headers['content-type'], 'image/png')
        self.assertEqual(first.headers['x-pdf-page-count'], '2')
        self.assertTrue(first.content.startswith(b'\x89PNG'))
        self.assertNotEqual(first.content, second.content)
        self.assertEqual(self.client.get(endpoint + '/preview?page=3').status_code, 404)
        self.assertEqual(self.client.get(endpoint + '/preview?page=0').status_code, 422)
        original = self.client.get(endpoint + '/file')
        self.assertIn('attachment', original.headers['content-disposition'])
        with pymupdf.open(stream=original.content, filetype='pdf') as document:
            self.assertEqual(document.page_count, 2)
        self.login('bob')
        self.assertEqual(self.client.get(endpoint + '/preview').status_code, 404)
        admin_endpoint = f'/api/admin/invoices/{invoice_id}/preview'
        self.assertEqual(self.client.get(admin_endpoint).status_code, 403)
        self.login('admin')
        self.assertEqual(self.client.get(admin_endpoint).status_code, 200)

    def test_auth_csrf_hash_and_role_cannot_be_supplied(self):
        self.assertEqual(self.client.get('/api/invoices').status_code, 401)
        payload = {'username': 'new-user', 'password': PASSWORD}
        self.assertEqual(self.client.post('/api/auth/register', json=payload).status_code, 403)
        self.assertEqual(self.mutate('POST', '/api/auth/register', json={**payload, 'role':'admin'}).status_code, 422)
        response = self.mutate('POST', '/api/auth/register', json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        self.csrf = response.json()['csrf_token']
        self.assertEqual(response.json()['user']['role'], 'user')
        self.assertNotIn('password_hash', response.text)
        with self.db() as db:
            encoded = db.execute("SELECT password_hash FROM users WHERE username = 'new-user'").fetchone()[0]
        self.assertNotEqual(encoded, PASSWORD)
        self.assertTrue(verify_password(PASSWORD, encoded))
        self.assertIn('httponly', response.headers['set-cookie'].lower())
        self.assertEqual(self.client.get('/api/admin/users').status_code, 403)
        logout = self.mutate('POST', '/api/auth/logout', json={})
        self.assertEqual(logout.status_code, 200)
        self.assertEqual(self.client.get('/api/users/me').status_code, 401)

    def test_validation_does_not_echo_password_and_session_cannot_be_forged(self):
        response = self.mutate('POST', '/api/auth/register', json={'username':'short', 'password':'private'})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn('private', response.text)
        self.client.cookies.set('easy_invoice_session', 'forged')
        self.assertEqual(self.client.get('/api/users/me').status_code, 401)

    def test_session_invalid_after_password_change_and_disable(self):
        self.login()
        response = self.mutate('PUT', '/api/users/me/password', json={'old_password':PASSWORD, 'new_password':'Changed-12345'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get('/api/users/me').status_code, 401)
        self.csrf = response.json()['csrf_token']
        result = self.mutate('POST', '/api/auth/login', json={'username':'alice', 'password':PASSWORD})
        self.assertEqual(result.status_code, 401)
        self.login('bob')
        with self.db() as db, db:
            db.execute('UPDATE users SET is_active = 0 WHERE id = 2')
        self.assertEqual(self.client.get('/api/invoices').status_code, 401)

    def test_user_isolation_all_record_types(self):
        foreign_title = self.title(2, 'Other Company')
        foreign_invoice = self.invoice(2, foreign_title)
        self.login('bob')
        foreign_plan = self.plan(foreign_title)
        job = self.mutate('POST', '/api/print/generate', json={
            'reimbursement_plan_id':foreign_plan['id'], 'invoice_ids':[foreign_invoice], 'auto_reimburse':False,
        }).json()
        self.login('alice')
        for path in (f'/api/invoices/{foreign_invoice}', f'/api/invoices/{foreign_invoice}/file', f"/api/print/{job['id']}/download"):
            self.assertEqual(self.client.get(path).status_code, 404, path)
        self.assertEqual(self.mutate('PUT',f'/api/invoices/{foreign_invoice}',json={'status':'excluded'}).status_code,404)
        self.assertEqual(self.mutate('DELETE',f'/api/invoices/{foreign_invoice}').status_code,404)
        self.assertEqual(self.mutate('PUT',f'/api/titles/{foreign_title}',json={'title_name':'stolen'}).status_code,404)
        self.assertEqual(self.mutate('POST','/api/reimbursements/optimize',json={'title_id':foreign_title,'target_amount':100}).status_code,404)
        self.assertEqual(self.mutate('POST','/api/print/generate',json={'reimbursement_plan_id':foreign_plan['id'],'invoice_ids':[foreign_invoice]}).status_code,404)
        self.assertEqual(self.client.get('/api/invoices').json()['total'],0)

    def test_filters_titles_statistics_and_manual_reimbursement(self):
        title = self.title()
        first = self.invoice(title_id=title, amount=100)
        self.invoice(title_id=title, amount=200, status='reimbursed')
        self.login()
        rows = self.client.get('/api/invoices', params={'title_id':title,'status':'active','amount_min':99,'amount_max':101,
                                                       'date_from':'2026-10-20','date_to':'2026-10-22'}).json()
        self.assertEqual([row['id'] for row in rows['items']], [first])
        self.assertNotIn('file_path', rows['items'][0])
        title_data = self.client.get('/api/titles').json()['items'][0]
        self.assertEqual((title_data['active_count'], title_data['active_amount']), (1,100))
        response = self.mutate('PUT',f'/api/invoices/{first}',json={'status':'reimbursed'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.client.get('/api/invoices/summary').json()['reimbursed']['count'],2)
        self.assertEqual(self.mutate('PUT',f'/api/invoices/{first}',json={'status':'active'}).status_code,200)
        self.assertEqual(self.mutate('DELETE',f'/api/titles/{title}').status_code,409)

    def test_manual_upload_correction_and_global_duplicate_check(self):
        self.login()
        response = self.mutate('POST','/api/invoices/upload',files={'file':('bill.pdf',sample_pdf(),'application/pdf')})
        self.assertEqual(response.status_code,201,response.text)
        invoice = response.json()
        self.assertEqual(invoice['status'],'pending')
        self.assertEqual(self.mutate('PUT',f"/api/invoices/{invoice['id']}",json={'status':'active'}).status_code,422)
        corrected = {'invoice_code':'012345678901','invoice_number':'00000001','invoice_date':'2026-10-21',
                     'buyer_name':'Manual Company','seller_name':'Seller','invoice_type':'普通发票','amount':'12.34','status':'active'}
        response = self.mutate('PUT',f"/api/invoices/{invoice['id']}",json=corrected)
        self.assertEqual(response.status_code,200,response.text)
        self.assertIsNotNone(response.json()['title_id'])
        self.assertIsNone(response.json()['ocr_error'])
        self.login('bob')
        second = self.mutate('POST','/api/invoices/upload',files={'file':('second.pdf',sample_pdf(),'application/pdf')}).json()
        response = self.mutate('PUT',f"/api/invoices/{second['id']}",json=corrected)
        self.assertEqual(response.status_code,409)
        self.assertIn('其他用户',response.json()['detail'])
        self.assertEqual(self.mutate('POST','/api/invoices/upload',files={'file':('bad.pdf',b'not-pdf','application/pdf')}).status_code,422)

    def test_file_path_is_not_publicly_settable_or_downloadable_outside_storage(self):
        key = self.invoice()
        self.login()
        self.assertEqual(self.mutate('PUT',f'/api/invoices/{key}',json={'file_path':'/etc/passwd'}).status_code,422)
        with self.db() as db, db:
            db.execute('UPDATE invoices SET file_path = ? WHERE id = ?', ('/etc/passwd',key))
        self.assertEqual(self.client.get(f'/api/invoices/{key}/file').status_code,404)

    def test_feishu_unbind_and_bind_guide(self):
        self.login()
        with self.db() as db, db:
            db.execute("UPDATE users SET feishu_open_id = 'ou_alice' WHERE id = 1")
        response = self.mutate('POST','/api/auth/feishu/bind',json={})
        self.assertEqual(response.json()['username'],'alice')
        self.assertTrue(response.json()['bound'])
        self.assertEqual(self.mutate('DELETE','/api/auth/feishu/bind').status_code,200)
        self.assertIsNone(self.client.get('/api/users/me').json()['feishu_open_id'])

    def test_delete_invoice_removes_it_from_list_and_recommendations(self):
        self.login()
        title = self.title()
        key = self.invoice(title_id=title)
        self.assertEqual(self.client.delete(f'/api/invoices/{key}').status_code, 403)
        self.assertEqual(self.mutate('DELETE', f'/api/invoices/{key}').status_code, 200)
        self.assertEqual(self.client.get(f'/api/invoices/{key}').status_code, 404)
        self.assertEqual(self.client.get('/api/invoices').json()['total'], 0)
        self.assertEqual(self.plan(title)['invoices'], [])

    def test_recommendation_default_and_shortfall_messages(self):
        self.login()
        title = self.title()
        lower = self.invoice(title_id=title, amount=99)
        higher = self.invoice(title_id=title, amount=120)
        result = self.mutate('POST', '/api/reimbursements/optimize',
                             json={'title_id': title, 'target_amount': 100}).json()
        self.assertTrue(result['allow_exceed'])
        self.assertTrue(result['target_met'])
        self.assertEqual([row['id'] for row in result['invoices']], [higher])
        result = self.plan(title, 100, False)
        self.assertFalse(result['target_met'])
        self.assertEqual([row['id'] for row in result['invoices']], [lower])
        self.assertIn('尚未允许超额', result['message'])
        result = self.plan(title, 300, True)
        self.assertEqual(len(result['invoices']), 2)
        self.assertFalse(result['target_met'])
        self.assertIn('81.00', result['message'])

    def test_optimize_and_pdf_default_auto_reimbursement(self):
        title = self.title()
        ids = [self.invoice(title_id=title,amount=amount) for amount in (70,60,40)]
        self.login()
        plan = self.plan(title)
        self.assertEqual({row['id'] for row in plan['invoices']}, set(ids[1:]))
        self.assertEqual(plan['total_amount'],100)
        response = self.mutate('POST','/api/print/generate',json={'reimbursement_plan_id':plan['id'],'invoice_ids':ids[1:]})
        self.assertEqual(response.status_code,201,response.text)
        self.assertTrue(response.json()['auto_reimburse'])
        pdf = self.client.get(response.json()['download_url'])
        self.assertEqual(pdf.status_code,200)
        with pymupdf.open(stream=pdf.content,filetype='pdf') as output:
            self.assertEqual(output.page_count,1)
            self.assertAlmostEqual(output[0].rect.width,595.2756,places=2)
            self.assertEqual(len(output[0].get_images()),1) # identical fixture images are deduplicated
        for key in ids[1:]:
            self.assertEqual(self.client.get(f'/api/invoices/{key}').json()['status'],'reimbursed')
        self.assertEqual(self.client.get(f'/api/invoices/{ids[0]}').json()['status'],'active')
        self.assertEqual(self.client.get('/api/print').json()['total'],1)

    def test_pdf_no_auto_and_checkbox_subset(self):
        title = self.title()
        ids = [self.invoice(title_id=title,amount=amount) for amount in (40,60)]
        self.login()
        plan = self.plan(title)
        response = self.mutate('POST','/api/print/generate',json={'reimbursement_plan_id':plan['id'],
                                                              'invoice_ids':[ids[0]],'auto_reimburse':False})
        self.assertEqual(response.status_code,201,response.text)
        self.assertEqual(response.json()['total_amount'],40)
        with self.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM invoices WHERE status = 'active'").fetchone()[0],2)
            snapshot = db.execute('SELECT selected_invoice_ids FROM reimbursement_plans WHERE id = '
                                  '(SELECT reimbursement_plan_id FROM print_jobs)').fetchone()[0]
            self.assertEqual(json.loads(snapshot),[ids[0]])

    def test_pdf_failure_and_mid_render_change_do_not_mark_reimbursed(self):
        title = self.title()
        key = self.invoice(title_id=title)
        self.login()
        plan = self.plan(title)
        payload = {'reimbursement_plan_id':plan['id'],'invoice_ids':[key]}
        with patch('app.api.print_jobs.generate_a4',side_effect=PdfGenerationError('test failure')):
            self.assertEqual(self.mutate('POST','/api/print/generate',json=payload).status_code,422)
        self.assertEqual(self.client.get(f'/api/invoices/{key}').json()['status'],'active')

        def conflicting_render(paths,target):
            generate_a4(paths,target)
            with self.db() as db, db:
                db.execute('UPDATE invoices SET amount = 99 WHERE id = ?', (key,))
        with patch('app.api.print_jobs.generate_a4',side_effect=conflicting_render):
            self.assertEqual(self.mutate('POST','/api/print/generate',json=payload).status_code,409)
        self.assertEqual(self.client.get('/api/print').json()['total'],0)
        self.assertEqual(list((self.root/'prints').rglob('*.pdf')),[])

    def test_admin_users_last_admin_logs_and_sensitive_fields(self):
        self.login('admin')
        users = self.client.get('/api/admin/users')
        self.assertEqual(users.status_code,200)
        self.assertNotIn('password_hash',users.text)
        self.assertEqual(self.mutate('PUT','/api/admin/users/3',json={'role':'user'}).status_code,409)
        self.assertEqual(self.mutate('PUT','/api/admin/users/1',json={'is_active':False}).status_code,200)
        logging.getLogger('app.test').warning('credential=%s',self.settings.feishu_app_secret)
        logs = self.client.get('/api/admin/logs').json()['lines']
        self.assertIn('[redacted]','\n'.join(logs))
        self.assertNotIn(self.settings.feishu_app_secret,'\n'.join(logs))
        self.assertEqual(self.client.get('/api/admin/logs',params={'source':'../../.env'}).status_code,422)
        self.assertEqual(self.client.get('/api/admin/invoices').status_code,200)

    def test_static_pages_and_security_headers(self):
        for path in ('/','/login','/invoices','/titles','/reimbursements','/prints','/settings','/admin','/static/app.js'):
            response = self.client.get(path)
            self.assertEqual(response.status_code,200,path)
            self.assertEqual(response.headers['x-frame-options'],'DENY')
        self.assertEqual(self.client.get('/api/auth/session').headers['cache-control'],'no-store')

    def test_parallel_dashboard_requests_use_request_scoped_connections(self):
        self.login()
        paths = ['/api/invoices', '/api/invoices/summary', '/api/titles', '/api/users/me'] * 5
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(self.client.get, paths))
        self.assertTrue(all(response.status_code == 200 for response in responses))


class OptimizationTests(unittest.TestCase):
    def test_matches_exhaustive_nonempty_subset_search(self):
        randomizer = random.Random(17)
        for _ in range(60):
            amounts = [randomizer.randrange(0,250) for _ in range(8)]
            target = randomizer.randrange(1,500)
            for exceed in (False,True):
                rows = [{'id':index+1,'amount':Decimal(amount)/100} for index,amount in enumerate(amounts)]
                valid=[]
                for length in range(1,len(rows)+1):
                    for indices in itertools.combinations(range(len(rows)),length):
                        total=sum(amounts[index] for index in indices)
                        if exceed or total<=target:
                            ids=tuple(index+1 for index in indices)
                            valid.append(((total<target,abs(total-target),len(ids),ids),list(ids)))
                expected=min(valid)[1] if valid else []
                self.assertEqual(optimize(rows,Decimal(target)/100,exceed),expected)

    def test_greedy_counterexample_exceed_tie_and_limit(self):
        rows=[{'id':1,'amount':70},{'id':2,'amount':60},{'id':3,'amount':40}]
        self.assertEqual(optimize(rows,100),[2,3])
        self.assertEqual(optimize([{'id':1,'amount':90},{'id':2,'amount':110}],100,True),[2])
        self.assertEqual(optimize([{'id':1,'amount':1000}],1,True),[1])
        self.assertEqual(optimize([{'id':1,'amount':1000}],1,False),[])
        with self.assertRaises(OptimizationLimitError):
            optimize(rows,100,max_states=1)


class BootstrapTests(unittest.TestCase):
    def test_env_admin_creation_does_not_reset_password_or_promote_existing_user(self):
        with tempfile.TemporaryDirectory() as directory:
            settings=Settings(Path(directory)/'app.db',bootstrap_admin_username='owner',bootstrap_admin_password=PASSWORD)
            initialize(settings.database_path)
            bootstrap_admin(settings)
            bootstrap_admin(replace(settings,bootstrap_admin_password='Different-password-123'))
            with closing(connect(settings.database_path)) as db, db:
                row=db.execute("SELECT * FROM users WHERE username = 'owner'").fetchone()
                self.assertEqual(row['role'],'admin')
                self.assertTrue(verify_password(PASSWORD,row['password_hash']))
                db.execute("INSERT INTO users(username,password_hash) VALUES ('ordinary',?)",(PASSWORD_HASH,))
            with self.assertRaises(ValueError):
                bootstrap_admin(replace(settings,bootstrap_admin_username='ordinary'))
