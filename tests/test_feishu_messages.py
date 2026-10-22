from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from lark_oapi.api.im.v1 import ReplyMessageResponse
import pymupdf

from app.db.connection import connect, initialize
from app.db.repositories.invoices import InvoiceRepository
from app.integrations.aliyun.invoice_ocr import parse_invoice_data
from app.integrations.feishu.dispatcher import EventDispatcher
from app.integrations.feishu.events import IncomingMessage
from app.integrations.feishu.handler import MessageHandler, MessageResult
from app.integrations.feishu.messages import FeishuMessageSender, ReplyError
from app.services.feishu_binding import BindingService
from app.services.invoice_ingestion import InvoiceIngestion
from app.services.storage import LocalStorage


def message_event(kind='text'):
    content = {'text': 'alice'} if kind == 'text' else {'file_name': 'invoice.pdf', 'file_key': 'file_key'}
    return {
        'schema': '2.0', 'header': {'event_type': 'im.message.receive_v1'},
        'event': {'sender': {'sender_type': 'user', 'sender_id': {'open_id': 'ou_alice'}},
                  'message': {'chat_type': 'p2p', 'message_type': kind, 'message_id': 'om_original',
                              'content': json.dumps(content)}},
    }


def api_response(code=0):
    response = ReplyMessageResponse()
    response.code = code
    return response


class SenderTests(unittest.TestCase):
    def test_sdk_request_json_and_stable_dedup_key(self):
        client = Mock()
        client.im.v1.message.reply.return_value = api_response()
        sender = FeishuMessageSender(client, sleep=Mock())
        content = '已收录发票。\n抬头：测试"公司"\\示例'
        sender.reply_text('om_1', content)
        first = client.im.v1.message.reply.call_args[0][0]
        self.assertEqual(first.message_id, 'om_1')
        self.assertEqual(first.uri, '/open-apis/im/v1/messages/:message_id/reply')
        self.assertEqual(first.request_body.msg_type, 'text')
        self.assertEqual(json.loads(first.request_body.content), {'text': content})
        self.assertFalse(first.request_body.reply_in_thread)
        self.assertLessEqual(len(first.request_body.uuid), 50)
        sender.reply_text('om_1', content)
        self.assertEqual(first.request_body.uuid, client.im.v1.message.reply.call_args[0][0].request_body.uuid)
        sender.reply_text('om_2', content)
        self.assertNotEqual(first.request_body.uuid, client.im.v1.message.reply.call_args[0][0].request_body.uuid)

    def test_retry_transport_and_api_failure_without_changing_uuid(self):
        client, sleep = Mock(), Mock()
        reply = client.im.v1.message.reply
        reply.side_effect = [TimeoutError('hidden'), api_response(999), api_response()]
        FeishuMessageSender(client, sleep=sleep).reply_text('om_1', '测试回复')
        self.assertEqual(reply.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])
        self.assertEqual(len({call.args[0].request_body.uuid for call in reply.call_args_list}), 1)

    def test_retry_limit_does_not_expose_provider_error(self):
        client = Mock()
        client.im.v1.message.reply.side_effect = ConnectionError('secret response')
        with self.assertRaisesRegex(ReplyError, '^feishu_reply_failed$'):
            FeishuMessageSender(client, sleep=Mock()).reply_text('om_1', '测试回复')
        self.assertEqual(client.im.v1.message.reply.call_count, 3)

    def test_validation_and_result_sink(self):
        client = Mock()
        client.im.v1.message.reply.return_value = api_response()
        sender = FeishuMessageSender(client, sleep=Mock())
        for message_id, text in [('', 'text'), ('om_1', ''), ('om_1', '  ')]:
            with self.assertRaises(ValueError):
                sender.reply_text(message_id, text)
        incoming = IncomingMessage('om_1', 'ou_1', 'text', text='alice')
        sender(incoming, MessageResult('ignored'))
        client.im.v1.message.reply.assert_not_called()
        sender(incoming, MessageResult('bound', '绑定成功', 1))
        self.assertEqual(client.im.v1.message.reply.call_args[0][0].message_id, 'om_1')


class ReplyRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.database = self.root / 'app.db'
        self.spool = self.root / 'events'
        initialize(self.database)
        with closing(connect(self.database)) as db, db:
            db.execute("INSERT INTO users(username, password_hash) VALUES ('alice', 'test-hash')")
        self.handler = MessageHandler(BindingService(self.database))

    def tearDown(self):
        self.temp.cleanup()

    def failed_sender(self):
        client = Mock()
        client.im.v1.message.reply.side_effect = TimeoutError('simulated')
        return FeishuMessageSender(client, sleep=Mock())

    def test_binding_reply_failure_recovers_original_success_without_rebinding(self):
        first = EventDispatcher(self.handler, self.failed_sender(), spool_dir=self.spool)
        self.assertTrue(first.submit(message_event()))
        first.close()
        self.assertEqual(len(list(self.spool.glob('*.json'))), 1)
        receipt = json.loads(next(self.spool.glob('*.result')).read_text())
        self.assertFalse(receipt['delivered'])
        self.assertEqual(receipt['result']['code'], 'bound')
        with closing(connect(self.database)) as db:
            self.assertEqual(db.execute('SELECT feishu_open_id FROM users').fetchone()[0], 'ou_alice')
        forbidden_handler = Mock()
        forbidden_handler.handle.side_effect = AssertionError('Business handling must not run again')
        client = Mock()
        client.im.v1.message.reply.return_value = api_response()
        second = EventDispatcher(forbidden_handler, FeishuMessageSender(client), spool_dir=self.spool)
        try:
            second.replay_pending()
            request = client.im.v1.message.reply.call_args[0][0]
            self.assertIn('绑定成功', json.loads(request.request_body.content)['text'])
            self.assertEqual(list(self.spool.glob('*.json')), [])
            self.assertFalse(second.submit(message_event()))
        finally:
            second.close()
        forbidden_handler.handle.assert_not_called()
        self.assertTrue(json.loads(next(self.spool.glob('*.result')).read_text())['delivered'])

    def test_invoice_reply_failure_does_not_repeat_download_ocr_or_insert(self):
        BindingService(self.database).bind('ou_alice', 'alice')
        with pymupdf.open() as document:
            document.new_page()
            binary = document.tobytes()
        downloader, ocr = Mock(), Mock()
        downloader.download.return_value = binary
        ocr.recognize.return_value = parse_invoice_data(json.dumps({'data': {
            'invoiceCode': '012345678901', 'invoiceNumber': '12345678', 'invoiceDate': '2026-10-21',
            'purchaserName': '测试抬头', 'sellerName': '测试销售方', 'totalAmount': '100.00',
            'invoiceType': '普通发票',
        }}))
        pipeline = InvoiceIngestion(InvoiceRepository(self.database), LocalStorage(self.root / 'invoices'),
                                    downloader, ocr)
        handler = MessageHandler(BindingService(self.database), pipeline)
        first = EventDispatcher(handler, self.failed_sender(), spool_dir=self.spool)
        first.submit(message_event('file'))
        first.close()
        client = Mock()
        client.im.v1.message.reply.return_value = api_response()
        second = EventDispatcher(handler, FeishuMessageSender(client), spool_dir=self.spool)
        try:
            second.replay_pending()
        finally:
            second.close()
        downloader.download.assert_called_once()
        ocr.recognize.assert_called_once()
        with closing(connect(self.database)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM invoices').fetchone()[0], 1)
        text = json.loads(client.im.v1.message.reply.call_args[0][0].request_body.content)['text']
        self.assertIn('已收录发票', text)
        self.assertIn('100.00', text)

    def test_recording_delivery_failure_preserves_stable_retry_key(self):
        client = Mock()
        client.im.v1.message.reply.return_value = api_response()
        first = EventDispatcher(self.handler, FeishuMessageSender(client), spool_dir=self.spool)
        save = first._save_result

        def disk_failure(message, result, delivered):
            if delivered:
                raise OSError('disk full after send')
            save(message, result, delivered)

        with patch.object(first, '_save_result', side_effect=disk_failure):
            first.submit(message_event())
            first.close()
        original_uuid = client.im.v1.message.reply.call_args[0][0].request_body.uuid
        second = EventDispatcher(self.handler, FeishuMessageSender(client), spool_dir=self.spool)
        try:
            second.replay_pending()
        finally:
            second.close()
        self.assertEqual(client.im.v1.message.reply.call_args[0][0].request_body.uuid, original_uuid)

    def test_corrupt_receipt_does_not_reexecute_business(self):
        first = EventDispatcher(self.handler, self.failed_sender(), spool_dir=self.spool)
        first.submit(message_event())
        first.close()
        next(self.spool.glob('*.result')).write_text('{"unexpected": true}')
        handler = Mock()
        second = EventDispatcher(handler, Mock(), spool_dir=self.spool)
        try:
            second.replay_pending()
        finally:
            second.close()
        handler.handle.assert_not_called()
        self.assertEqual(len(list(self.spool.glob('*.json'))), 1)


if __name__ == '__main__':
    unittest.main()
