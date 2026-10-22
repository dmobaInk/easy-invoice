"""Business dispatch with injectable OCR and reply ports for steps 4 and 5."""

from dataclasses import dataclass
from typing import Callable, Optional

from app.integrations.feishu.events import IncomingMessage
from app.services.feishu_binding import BindingService


@dataclass(frozen=True)
class MessageResult:
    code: str
    reply_text: Optional[str] = None
    user_id: Optional[int] = None


FileProcessor = Callable[[int, IncomingMessage], MessageResult]
ResultSink = Callable[[IncomingMessage, MessageResult], None]


class MessageHandler:
    def __init__(self, binding: BindingService, file_processor: Optional[FileProcessor] = None):
        self.binding = binding
        self.file_processor = file_processor

    def handle(self, message: IncomingMessage) -> MessageResult:
        if message.message_type == 'text':
            result = self.binding.bind(message.open_id, message.text or '')
            return MessageResult(result.code, result.text, result.user_id)
        if not (message.file_name or '').lower().endswith('.pdf'):
            return MessageResult('unsupported_file', '仅支持 PDF 发票，请发送 PDF 文件。')
        user_id = self.binding.find_active_user(message.open_id)
        if user_id is None:
            return MessageResult('binding_required', '请先发送 Web 端已注册的用户名完成绑定。')
        if self.file_processor is None:
            # Do not report successful ingestion before step 4 is implemented.
            return MessageResult('pipeline_not_ready', '发票处理功能尚未启用，请稍后重新发送。', user_id)
        return self.file_processor(user_id, message)
