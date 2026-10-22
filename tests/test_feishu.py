from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import warnings

from app.config import PROJECT_ROOT, Settings
from app.db.connection import connect, initialize
from app.integrations.feishu.dispatcher import EventDispatcher
from app.integrations.feishu.events import parse_event
from app.integrations.feishu.handler import MessageHandler, MessageResult
from app.services.feishu_binding import BindingService


def event(kind='text', content=None, message_id='om_1', open_id='ou_alice'):
    if content is None:
        content = {'text': ' alice '} if kind == 'text' else {'file_name': 'invoice.PDF', 'file_key': 'key_1'}
    return {
        'schema': '2.0',
        'header': {'event_type': 'im.message.receive_v1', 'event_id': 'evt_1'},
        'event': {
            'sender': {'sender_type': 'user', 'sender_id': {'open_id': open_id}},
            'message': {'chat_type': 'p2p', 'message_type': kind, 'message_id': message_id,
                        'content': json.dumps(content)},
        },
    }


class FeishuTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'db.sqlite'
        initialize(self.path)
        with closing(connect(self.path)) as db, db:
            db.executemany('INSERT INTO users(username, password_hash) VALUES (?, ?)',
                           [('alice', 'test-hash'), ('bob', 'test-hash'), ('disabled', 'test-hash')])
            db.execute("UPDATE users SET is_active = 0 WHERE username = 'disabled'")
        self.binding = BindingService(self.path)
        self.handler = MessageHandler(self.binding)

    def tearDown(self):
        self.temp.cleanup()

    def test_exact_username_matching_and_missing_account(self):
        message = parse_event(event())
        self.assertEqual(message.text, 'alice')
        self.assertEqual(self.handler.handle(message).code, 'bound')
        self.assertEqual(self.binding.bind('ou_other', 'hello bob').code, 'user_not_found')
        self.assertEqual(self.binding.bind('ou_other', 'ALICE').code, 'user_not_found')
        with closing(connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM users').fetchone()[0], 3)

    def test_binding_conflicts_disabled_and_unbind(self):
        self.assertEqual(self.binding.bind('ou_alice', 'alice').code, 'bound')
        self.assertEqual(self.binding.bind('ou_alice', 'alice').code, 'already_bound')
        self.assertEqual(self.binding.bind('ou_other', 'alice').code, 'account_bound')
        self.assertEqual(self.binding.bind('ou_alice', 'bob').code, 'sender_bound')
        self.assertEqual(self.binding.bind('ou_other', 'disabled').code, 'user_disabled')
        with closing(connect(self.path)) as db, db:
            db.execute("UPDATE users SET feishu_open_id = NULL WHERE username = 'alice'")
        self.assertEqual(self.binding.bind('ou_alice', 'bob').code, 'bound')

    def test_concurrent_binding_only_one_winner(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda oid: self.binding.bind(oid, 'alice').code,
                                    ['ou_first', 'ou_second']))
        self.assertCountEqual(results, ['bound', 'account_bound'])

    def test_group_bot_and_other_types_ignored(self):
        for field, value in [('chat_type', 'group'), ('message_type', 'image')]:
            payload = event()
            payload['event']['message'][field] = value
            self.assertIsNone(parse_event(payload))
        payload = event()
        payload['event']['sender']['sender_type'] = 'app'
        self.assertIsNone(parse_event(payload))
        payload = event()
        payload['header']['event_type'] = 'other.event'
        self.assertIsNone(parse_event(payload))

    def test_invalid_payloads(self):
        for content in ('not-json', '[]', '{}', '{"text": 123}'):
            payload = event()
            payload['event']['message']['content'] = content
            with self.assertRaises(ValueError):
                parse_event(payload)
        payload = event()
        del payload['event']['sender']['sender_id']['open_id']
        with self.assertRaises(ValueError):
            parse_event(payload)

    def test_file_routing_requires_binding_and_pdf(self):
        message = parse_event(event('file'))
        self.assertEqual(self.handler.handle(message).code, 'binding_required')
        self.binding.bind('ou_alice', 'alice')
        self.assertEqual(self.handler.handle(message).code, 'pipeline_not_ready')
        processor = Mock(return_value=MessageResult('processed'))
        handler = MessageHandler(self.binding, processor)
        self.assertEqual(handler.handle(message).code, 'processed')
        processor.assert_called_once_with(1, message)
        unsupported = parse_event(event('file', {'file_name': 'invoice.exe', 'file_key': 'key'}))
        self.assertEqual(handler.handle(unsupported).code, 'unsupported_file')
        with closing(connect(self.path)) as db, db:
            db.execute('UPDATE users SET is_active = 0 WHERE id = 1')
        self.assertEqual(handler.handle(message).code, 'binding_required')

    def test_dispatcher_inflight_duplicate_and_backpressure(self):
        gate, started, delivered = threading.Event(), threading.Event(), threading.Event()
        handler = Mock()

        def process(message):
            started.set()
            gate.wait(3)
            return MessageResult('test')

        handler.handle.side_effect = process
        dispatcher = EventDispatcher(handler, lambda m, r: delivered.set(), workers=1, capacity=1)
        try:
            self.assertTrue(dispatcher.submit(event()))
            self.assertTrue(started.wait(2))
            self.assertFalse(dispatcher.submit(event()))
            with self.assertRaises(RuntimeError):
                dispatcher.submit(event(message_id='om_2'))
        finally:
            gate.set()
            dispatcher.close()
        self.assertTrue(delivered.is_set())
        self.assertEqual(handler.handle.call_count, 1)

    def test_completion_cache_and_expiry(self):
        now = [0.0]
        sink = Mock()
        dispatcher = EventDispatcher(self.handler, sink, ttl=10, clock=lambda: now[0])
        try:
            self.assertTrue(dispatcher.submit(event()))
            dispatcher.executor.shutdown(wait=True)
            self.assertFalse(dispatcher.submit(event()))
            now[0] = 11
            dispatcher.executor = ThreadPoolExecutor(max_workers=1)
            self.assertTrue(dispatcher.submit(event()))
        finally:
            dispatcher.close()
        self.assertEqual(sink.call_count, 2)

    def test_acknowledged_events_persist_and_replay_after_failure(self):
        spool = Path(self.temp.name) / 'events'
        failed = Mock()
        failed.handle.side_effect = RuntimeError('simulated')
        first = EventDispatcher(failed, Mock(), spool_dir=spool)
        self.assertTrue(first.submit(event()))
        first.close()
        self.assertEqual(len(list(spool.glob('*.json'))), 1)
        sink = Mock()
        second = EventDispatcher(self.handler, sink, spool_dir=spool)
        try:
            second.replay_pending()
        finally:
            second.close()
        self.assertEqual(sink.call_args[0][1].code, 'bound')
        self.assertEqual(list(spool.glob('*.json')), [])

    def test_disk_failure_does_not_acknowledge(self):
        dispatcher = EventDispatcher(self.handler, Mock(), spool_dir=Path(self.temp.name) / 'events')
        try:
            with patch.object(dispatcher, '_persist', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    dispatcher.submit(event())
            self.assertFalse(dispatcher.inflight)
        finally:
            dispatcher.close()

    def test_sdk_model_roundtrip_and_registration(self):
        try:
            from app.integrations.feishu.listener import build_event_handler
            import lark_oapi
        except ImportError:
            self.skipTest('Install project dependencies for SDK integration test')
        sink = Mock()
        dispatcher = EventDispatcher(self.handler, sink)
        sdk_handler = build_event_handler(dispatcher)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', DeprecationWarning)
                sdk_handler.do_without_validation(json.dumps(event()).encode())
        finally:
            dispatcher.close()
        self.assertEqual(sink.call_args[0][1].code, 'bound')


class SettingsTests(unittest.TestCase):
    def test_credentials_required_and_secrets_hidden(self):
        with patch.dict(os.environ, {'FEISHU_ENABLED': 'true'}, clear=True):
            with self.assertRaises(ValueError):
                Settings.from_env(env_file=None)
        with patch.dict(os.environ, {'FEISHU_ENABLED': 'true', 'FEISHU_APP_ID': 'test_id',
                                    'FEISHU_APP_SECRET': 'never-print-this'}, clear=True):
            settings = Settings.from_env(env_file=None)
            self.assertTrue(settings.feishu_enabled)
            self.assertNotIn('never-print-this', repr(settings))
            self.assertEqual(settings.database_path, PROJECT_ROOT / 'data/app.db')

    def test_dotenv_loading_environment_precedence_and_literal_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            path.write_text('FEISHU_ENABLED=true\nFEISHU_APP_ID=file_id\n'
                            'FEISHU_APP_SECRET="literal-${SECRET_VARIABLE}"\n', encoding='utf-8')
            with patch.dict(os.environ, {'FEISHU_APP_ID': 'environment_id'}, clear=True):
                settings = Settings.from_env(env_file=path)
                self.assertTrue(settings.feishu_enabled)
                self.assertEqual(settings.feishu_app_id, 'environment_id')
                self.assertEqual(settings.feishu_app_secret, 'literal-${SECRET_VARIABLE}')
                self.assertNotIn('FEISHU_APP_SECRET', os.environ)
            with patch.dict(os.environ, {'FEISHU_APP_SECRET': ''}, clear=True):
                with self.assertRaises(ValueError):
                    Settings.from_env(env_file=path)


if __name__ == '__main__':
    unittest.main()
