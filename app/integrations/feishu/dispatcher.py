"""Bound callback work so the SDK can acknowledge quickly and maintain heartbeats."""

from collections import OrderedDict
from dataclasses import asdict
from concurrent.futures import ThreadPoolExecutor
import logging
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Callable

from app.integrations.feishu.events import parse_event
from app.integrations.feishu.handler import MessageHandler, MessageResult, ResultSink

logger = logging.getLogger(__name__)


class EventDispatcher:
    def __init__(self, handler: MessageHandler, sink: ResultSink, *, workers: int = 2,
                 capacity: int = 128, cache_size: int = 4096, ttl: float = 3600,
                 clock: Callable[[], float] = time.monotonic, spool_dir=None):
        if min(workers, capacity, cache_size) < 1 or ttl <= 0:
            raise ValueError('Dispatcher limits must be positive')
        self.handler, self.sink = handler, sink
        self.clock, self.ttl, self.cache_size = clock, ttl, cache_size
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='feishu-event')
        self.slots = threading.BoundedSemaphore(capacity)
        self.lock = threading.Lock()
        self.inflight: set[str] = set()
        self.completed: OrderedDict[str, float] = OrderedDict()
        self.closed = False
        self.spool_dir = Path(spool_dir) if spool_dir is not None else None
        if self.spool_dir is not None:
            self.spool_dir.mkdir(parents=True, exist_ok=True)

    def _spool_path(self, message_id):
        return self.spool_dir / (hashlib.sha256(message_id.encode()).hexdigest() + '.json')

    def _result_path(self, message_id):
        return self._spool_path(message_id).with_suffix('.result')

    def _write_json(self, target, payload):
        name = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.spool_dir,
                                             suffix='.tmp', delete=False) as stream:
                name = stream.name
                json.dump(payload, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, target)
        finally:
            if name is not None and os.path.exists(name):
                os.unlink(name)

    def _persist(self, message_id, payload):
        if self.spool_dir is not None:
            self._write_json(self._spool_path(message_id), payload)

    def _save_result(self, message, result, delivered):
        if self.spool_dir is not None:
            self._write_json(self._result_path(message.message_id), {
                'message_id': message.message_id, 'open_id': message.open_id,
                'result': asdict(result), 'delivered': delivered,
            })

    def _load_result(self, message):
        if self.spool_dir is None:
            return None
        path = self._result_path(message.message_id)
        if not path.exists():
            return None
        receipt = json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(receipt, dict) or receipt.get('message_id') != message.message_id
                or receipt.get('open_id') != message.open_id
                or type(receipt.get('delivered')) is not bool):
            raise ValueError('Invalid reply receipt identity')
        result = receipt.get('result')
        if (not isinstance(result, dict) or set(result) != {'code', 'reply_text', 'user_id'}
                or not isinstance(result.get('code'), str)
                or (result.get('reply_text') is not None and not isinstance(result['reply_text'], str))
                or (result.get('user_id') is not None and type(result['user_id']) is not int)):
            raise ValueError('Invalid reply receipt result')
        return MessageResult(**result), receipt['delivered']

    def replay_pending(self):
        """Replay persisted events before accepting new SDK callbacks.

        Called once during startup, sequentially, so backlog size cannot exhaust
        the in-memory queue. Failed events stay on disk for a resend or restart.
        """
        if self.spool_dir is None:
            return
        for path in sorted(self.spool_dir.glob('*.json')):
            try:
                message = parse_event(json.loads(path.read_text(encoding='utf-8')))
                if message is None or path != self._spool_path(message.message_id):
                    raise ValueError('Invalid persisted event')
            except (ValueError, OSError):
                logger.error('Invalid persisted Feishu event; retained for inspection')
                continue
            self.slots.acquire()
            with self.lock:
                self.inflight.add(message.message_id)
            self._process(message)

    def submit(self, payload: dict) -> bool:
        try:
            message = parse_event(payload)
        except ValueError:
            logger.warning('Ignoring malformed Feishu message event')
            return False
        if message is None:
            return False
        with self.lock:
            if self.closed:
                raise RuntimeError('Feishu dispatcher is closed')
            now = self.clock()
            while self.completed and next(iter(self.completed.values())) <= now:
                self.completed.popitem(last=False)
            if message.message_id in self.inflight or message.message_id in self.completed:
                return False
            receipt = self._load_result(message)
            if receipt is not None and receipt[1]:
                return False
            if not self.slots.acquire(blocking=False):
                # Propagate through the SDK callback so it does not acknowledge success.
                raise RuntimeError('Feishu event capacity exhausted; retry delivery')
            self.inflight.add(message.message_id)
            try:
                # Persist before returning success to the SDK acknowledgement.
                self._persist(message.message_id, payload)
                self.executor.submit(self._process, message)
            except BaseException:
                self.inflight.remove(message.message_id)
                self.slots.release()
                raise
        return True

    def _process(self, message):
        success = False
        try:
            receipt = self._load_result(message)
            if receipt is None:
                result = self.handler.handle(message)
                self._save_result(message, result, delivered=False)
                delivered = False
            else:
                result, delivered = receipt
            if not delivered:
                self.sink(message, result)
                self._save_result(message, result, delivered=True)
            if self.spool_dir is not None:
                self._spool_path(message.message_id).unlink(missing_ok=True)
            success = True
        except Exception:
            # Do not include potentially sensitive event payloads or exception text.
            logger.error('Feishu message handling or reply failed; persisted work retained for restart')
        finally:
            with self.lock:
                self.inflight.remove(message.message_id)
                if success:
                    self.completed[message.message_id] = self.clock() + self.ttl
                    while len(self.completed) > self.cache_size:
                        self.completed.popitem(last=False)
            self.slots.release()

    def close(self):
        with self.lock:
            self.closed = True
        self.executor.shutdown(wait=True)
