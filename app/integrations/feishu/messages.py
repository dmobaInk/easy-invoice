"""Reply to an incoming message; the official SDK handles tenant token caching."""

import json
import logging
import time
import uuid

from app.integrations.feishu.events import IncomingMessage
from app.integrations.feishu.handler import MessageResult

logger = logging.getLogger(__name__)


class ReplyError(Exception):
    pass


class FeishuMessageSender:
    def __init__(self, client, *, sleep=time.sleep):
        self.client, self.sleep = client, sleep

    def reply_text(self, message_id: str, text: str) -> None:
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

        if not isinstance(message_id, str) or not message_id.strip():
            raise ValueError('message_id is required')
        if not isinstance(text, str) or not text.strip():
            raise ValueError('reply text is required')
        # Stable across attempts/restarts; UUID length is 36 characters.
        request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'easy-invoice:reply:' + message_id))
        body = (ReplyMessageRequestBody.builder().msg_type('text')
                .content(json.dumps({'text': text}, ensure_ascii=False))
                .reply_in_thread(False).uuid(request_id).build())
        request = ReplyMessageRequest.builder().message_id(message_id).request_body(body).build()
        for attempt in range(3):
            try:
                response = self.client.im.v1.message.reply(request)
                if not response.success():
                    # Log the numeric API code, never provider text or tokens.
                    code = response.code if isinstance(response.code, int) else 'unknown'
                    logger.warning('Feishu reply rejected: code=%s', code)
                    raise ReplyError('feishu_reply_rejected')
                return
            except Exception:
                logger.warning('Feishu reply failed: attempt=%d', attempt + 1)
                if attempt == 2:
                    raise ReplyError('feishu_reply_failed') from None
                self.sleep(2 ** attempt)

    def __call__(self, message: IncomingMessage, result: MessageResult) -> None:
        if result.reply_text is not None:
            self.reply_text(message.message_id, result.reply_text)
        logger.info('Feishu event handled: result=%s', result.code)
