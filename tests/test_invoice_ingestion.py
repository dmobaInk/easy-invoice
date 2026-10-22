from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from decimal import Decimal
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import pymupdf

from app.config import Settings
from app.db.connection import connect, initialize
from app.db.repositories.invoices import InvoiceRepository
from app.integrations.aliyun.invoice_ocr import (
    AliyunInvoiceOcr, OcrError, parse_invoice_data,
)
from app.integrations.feishu.events import IncomingMessage
from app.integrations.feishu.files import (
    DownloadError, FeishuFileDownloader, FileTooLargeError, MAX_PDF_BYTES,
)
from app.services.invoice_ingestion import InvoiceIngestion
from app.services.storage import InvalidPdfError, LocalStorage, validate_pdf


def pdf_bytes(pages=1, encrypted=False):
    with pymupdf.open() as document:
        for index in range(pages):
            document.new_page().insert_text((72, 72), f'Test invoice page {index + 1}')
        return document.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw='owner', user_pw='user') \
            if encrypted else document.tobytes()


def ocr_json(**overrides):
    data = {'invoiceCode': '031001600211', 'invoiceNumber': '67516978',
            'invoiceDate': '2017年02月12日', 'purchaserName': '测试购买方',
            'purchaserTaxNumber': 'TEST_TAX_ID', 'sellerName': '测试销售方',
            'totalAmount': '88.00', 'invoiceType': '电子普通发票'}
    data.update(overrides)
    return json.dumps({'data': data})


class IngestionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.database = self.root / 'app.db'
        initialize(self.database)
        with closing(connect(self.database)) as db, db:
            db.executemany('INSERT INTO users(username, password_hash, feishu_open_id) VALUES (?, ?, ?)',
                           [('alice', 'hash', 'ou_1'), ('bob', 'hash', 'ou_2')])
        self.repository = InvoiceRepository(self.database)
        self.storage = LocalStorage(self.root / 'invoices')
        self.pdf = pdf_bytes(pages=2)
        self.downloader = Mock()
        self.downloader.download.return_value = self.pdf
        self.ocr = Mock()
        self.ocr.recognize.return_value = parse_invoice_data(ocr_json())
        self.pipeline = InvoiceIngestion(self.repository, self.storage, self.downloader, self.ocr)
        self.message = IncomingMessage('om_1', 'ou_1', 'file', file_key='file_key', file_name='../bill.pdf')

    def tearDown(self):
        self.temp.cleanup()

    def rows(self, table='invoices'):
        with closing(connect(self.database)) as db:
            return [dict(row) for row in db.execute('SELECT * FROM ' + table)]

    def test_record_title_and_original_all_pages_retained(self):
        result = self.pipeline(1, self.message)
        self.assertEqual(result.code, 'invoice_recorded')
        invoice = self.rows()[0]
        self.assertEqual(invoice['status'], 'active')
        self.assertEqual(invoice['amount'], 88.0)
        self.assertEqual(invoice['invoice_date'], '2017-02-12')
        self.assertEqual(invoice['invoice_code'], '031001600211')
        self.assertEqual(invoice['title_id'], self.rows('invoice_titles')[0]['id'])
        path = Path(invoice['file_path'])
        self.assertEqual(path.parent, self.root / 'invoices' / '1')
        self.assertEqual(path.read_bytes(), self.pdf)
        self.assertEqual(validate_pdf(path.read_bytes()), 2)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_redelivery_avoids_ocr_and_download(self):
        self.pipeline(1, self.message)
        self.assertEqual(self.pipeline(1, self.message).code, 'invoice_recorded')
        self.downloader.download.assert_called_once()
        self.ocr.recognize.assert_called_once()
        self.assertEqual(len(self.rows()), 1)

    def test_duplicate_same_and_other_user(self):
        self.pipeline(1, self.message)
        same = self.pipeline(1, replace(self.message, message_id='om_same'))
        other = self.pipeline(2, replace(self.message, message_id='om_other', open_id='ou_2'))
        self.assertEqual(same.code, 'duplicate_same_user')
        self.assertEqual(other.code, 'duplicate_other_user')
        self.assertIn('其他用户', other.reply_text)
        self.assertNotIn('测试购买方', other.reply_text)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(self.rows('invoice_titles')), 1)
        self.assertTrue(Path(self.rows()[0]['file_path']).is_file())
        self.assertFalse(self.storage.invoice_path(2, 'om_other').exists())

    def test_concurrent_global_deduplication(self):
        inputs = [(1, self.message), (2, replace(self.message, message_id='om_2', open_id='ou_2'))]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda args: self.pipeline(*args).code, inputs))
        self.assertCountEqual(results, ['invoice_recorded', 'duplicate_other_user'])
        self.assertEqual(len(self.rows()), 1)

    def test_ocr_failure_retains_pending_and_redelivery_does_not_rebill(self):
        self.ocr.recognize.side_effect = OcrError('ocr_recognition_failed')
        self.assertEqual(self.pipeline(1, self.message).code, 'ocr_pending')
        row = self.rows()[0]
        self.assertEqual(row['status'], 'pending')
        self.assertIsNone(row['invoice_code'])
        self.assertEqual(row['ocr_error'], 'ocr_recognition_failed')
        self.assertTrue(Path(row['file_path']).is_file())
        self.pipeline(1, self.message)
        self.ocr.recognize.assert_called_once()

    def test_incomplete_fields_pending_preserves_known_identity(self):
        self.ocr.recognize.return_value = parse_invoice_data(ocr_json(totalAmount='NaN'))
        self.assertEqual(self.pipeline(1, self.message).code, 'ocr_pending')
        row = self.rows()[0]
        self.assertEqual(row['invoice_number'], '67516978')
        self.assertIsNone(row['amount'])
        self.assertIn('amount', row['ocr_error'])

    def test_invalid_encrypted_download_and_size_failures_do_not_insert(self):
        for content in (b'not a pdf', b'%PDF-corrupt', pdf_bytes(encrypted=True)):
            self.downloader.download.return_value = content
            self.assertEqual(self.pipeline(1, self.message).code, 'invalid_pdf')
        self.ocr.recognize.assert_not_called()
        self.downloader.download.side_effect = DownloadError('failed')
        self.assertEqual(self.pipeline(1, self.message).code, 'download_failed')
        self.downloader.download.side_effect = FileTooLargeError('large')
        self.assertEqual(self.pipeline(1, self.message).code, 'file_too_large')
        self.assertEqual(self.rows(), [])

    def test_digital_invoice_recorded_and_deduplicated(self):
        self.ocr.recognize.return_value = parse_invoice_data(ocr_json(
            invoiceCode='', invoiceNumber='12345678901234567890', invoiceType='数电普通发票'))
        self.assertEqual(self.pipeline(1, self.message).code, 'invoice_recorded')
        row = self.rows()[0]
        self.assertIsNone(row['invoice_code'])
        self.assertEqual(row['status'], 'active')
        other = replace(self.message, message_id='other_message', open_id='ou_2')
        self.assertEqual(self.pipeline(2, other).code, 'duplicate_other_user')
        self.assertEqual(len(self.rows()), 1)

    def test_revoke_binding_during_ocr(self):
        def recognize(path):
            with closing(connect(self.database)) as db, db:
                db.execute('UPDATE users SET feishu_open_id = NULL WHERE id = 1')
            return parse_invoice_data(ocr_json())
        self.ocr.recognize.side_effect = recognize
        self.assertEqual(self.pipeline(1, self.message).code, 'binding_changed')
        self.assertEqual(self.rows(), [])

    def test_message_id_cannot_move_to_another_user(self):
        self.pipeline(1, self.message)
        result = self.pipeline(2, replace(self.message, open_id='ou_2'))
        self.assertEqual(result.code, 'message_owner_changed')
        self.assertNotIn('测试购买方', result.reply_text)
        self.assertEqual(len(self.rows()), 1)

    def test_resume_original_after_interruption(self):
        path = self.storage.save_invoice(1, self.message.message_id, self.pdf)
        self.assertEqual(self.pipeline(1, self.message).code, 'invoice_recorded')
        self.downloader.download.assert_not_called()
        self.ocr.recognize.assert_called_once_with(path)


class OcrAdapterTests(unittest.TestCase):
    def test_parse_fields_dates_and_exact_amount(self):
        data = parse_invoice_data(ocr_json(totalAmount='￥1,234.50', invoiceDate='2026/10/21'))
        self.assertEqual(data.amount, Decimal('1234.50'))
        self.assertEqual(data.invoice_date, '2026-10-21')
        self.assertIsNone(data.validation_error)
        for value in ('-1', 'Infinity', 'NaN', '12.345', 'unknown', True):
            self.assertIsNone(parse_invoice_data(ocr_json(totalAmount=value)).amount)
        self.assertIsNone(parse_invoice_data(ocr_json(invoiceDate='2026-02-30')).invoice_date)
        for raw in ('not-json', '{}', '[]', '{"data": []}'):
            with self.assertRaises(OcrError):
                parse_invoice_data(raw)

    def test_accept_digital_invoice(self):
        for overrides in ({'invoiceType': '数电普通发票'},
                          {'invoiceCode': '', 'invoiceNumber': '12345678901234567890'}):
            self.assertIsNone(parse_invoice_data(ocr_json(**overrides)).validation_error)

    def test_sdk_first_page_pdf_binary_and_retry_reopens_stream(self):
        from alibabacloud_ocr_api20210707.models import RecognizeInvoiceResponse, RecognizeInvoiceResponseBody

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'input.pdf'
            binary = pdf_bytes(pages=2)
            path.write_bytes(binary)
            client, sleep = Mock(), Mock()
            attempts = []

            def invoke(request, runtime):
                self.assertEqual(request.page_no, 1)
                self.assertIsNone(request.url)
                self.assertFalse(runtime.autoretry)
                attempts.append(request.body.read())
                if len(attempts) == 1:
                    raise TimeoutError('simulated')
                return RecognizeInvoiceResponse(body=RecognizeInvoiceResponseBody(data=ocr_json()))

            client.recognize_invoice_with_options.side_effect = invoke
            adapter = AliyunInvoiceOcr(Settings(path), client=client, sleep=sleep)
            self.assertEqual(adapter.recognize(path).amount, Decimal('88.00'))
            self.assertEqual(attempts, [binary, binary])
            sleep.assert_called_once_with(1)

    def test_ocr_retry_limit_missing_credentials_and_body_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'invoice.pdf'
            path.write_bytes(pdf_bytes())
            client, sleep = Mock(), Mock()
            client.recognize_invoice_with_options.side_effect = ConnectionError('do not leak response')
            adapter = AliyunInvoiceOcr(Settings(path), client=client, sleep=sleep)
            with self.assertRaisesRegex(OcrError, '^ocr_recognition_failed$'):
                adapter.recognize(path)
            self.assertEqual(client.recognize_invoice_with_options.call_count, 3)
            self.assertEqual(sleep.call_count, 2)
            with self.assertRaisesRegex(OcrError, '^aliyun_credentials_missing$'):
                AliyunInvoiceOcr(Settings(path), sleep=sleep).recognize(path)

    def test_sdk_error_body_is_not_success(self):
        from alibabacloud_ocr_api20210707.models import RecognizeInvoiceResponse, RecognizeInvoiceResponseBody

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'invoice.pdf'
            path.write_bytes(pdf_bytes())
            client = Mock()
            client.recognize_invoice_with_options.return_value = RecognizeInvoiceResponse(
                body=RecognizeInvoiceResponseBody(code='noPermission', data=ocr_json()))
            with self.assertRaises(OcrError):
                AliyunInvoiceOcr(Settings(path), client=client, sleep=Mock()).recognize(path)
            self.assertEqual(client.recognize_invoice_with_options.call_count, 3)


class DownloadAdapterTests(unittest.TestCase):
    @staticmethod
    def response(data):
        from lark_oapi.api.im.v1 import GetMessageResourceResponse
        response = GetMessageResourceResponse()
        response.code, response.file = 0, io.BytesIO(data)
        return response

    def test_message_resource_request_and_retry(self):
        client, sleep = Mock(), Mock()
        response = self.response(b'pdf data')
        get = client.im.v1.message_resource.get
        get.side_effect = [TimeoutError('simulated'), response]
        downloader = FeishuFileDownloader(client, sleep=sleep)
        self.assertEqual(downloader.download('om_123', 'file_key_123'), b'pdf data')
        request = get.call_args[0][0]
        self.assertEqual(request.message_id, 'om_123')
        self.assertEqual(request.file_key, 'file_key_123')
        self.assertEqual(request.type, 'file')
        self.assertTrue(response.file.closed)
        sleep.assert_called_once_with(1)

    def test_download_retry_limit_and_size_limit(self):
        client, sleep = Mock(), Mock()
        get = client.im.v1.message_resource.get
        get.side_effect = ConnectionError('simulated')
        with self.assertRaises(DownloadError):
            FeishuFileDownloader(client, sleep=sleep).download('om_1', 'key')
        self.assertEqual(get.call_count, 3)
        get.reset_mock(side_effect=True)
        response = self.response(b'x' * (MAX_PDF_BYTES + 1))
        get.return_value = response
        with self.assertRaises(FileTooLargeError):
            FeishuFileDownloader(client, sleep=Mock()).download('om_1', 'key')
        get.assert_called_once()
        self.assertTrue(response.file.closed)

    def test_resource_api_error_retries(self):
        client = Mock()
        response = self.response(b'')
        response.code, response.file = 999, None
        client.im.v1.message_resource.get.return_value = response
        with self.assertRaises(DownloadError):
            FeishuFileDownloader(client, sleep=Mock()).download('om_1', 'key')
        self.assertEqual(client.im.v1.message_resource.get.call_count, 3)


if __name__ == '__main__':
    unittest.main()
