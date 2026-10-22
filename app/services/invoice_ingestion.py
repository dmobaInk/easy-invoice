"""Download -> validate/store -> OCR first page -> atomic deduplicate/insert."""

import logging
from pathlib import Path

from app.config import Settings
from app.db.repositories.invoices import InvoiceRepository, SaveResult
from app.integrations.aliyun.invoice_ocr import (
    AliyunInvoiceOcr, OcrError, RecognizedInvoice,
)
from app.integrations.feishu.client import build_api_client
from app.integrations.feishu.events import IncomingMessage
from app.integrations.feishu.files import DownloadError, FeishuFileDownloader, FileTooLargeError
from app.integrations.feishu.handler import MessageResult
from app.services.storage import InvalidPdfError, LocalStorage

logger = logging.getLogger(__name__)


def recognize_or_pending(ocr, path):
    try:
        data = ocr.recognize(path)
        return data, data.validation_error
    except OcrError as exc:
        logger.error('Invoice OCR failed; original PDF retained as pending')
        return RecognizedInvoice(), str(exc)


class UploadIngestion:
    def __init__(self, repository: InvoiceRepository, ocr):
        self.repository, self.ocr = repository, ocr

    def __call__(self, user_id: int, path: Path) -> SaveResult:
        data, error = recognize_or_pending(self.ocr, path)
        return self.repository.save_upload(user_id, path, data, error)


class InvoiceIngestion:
    def __init__(self, repository: InvoiceRepository, storage: LocalStorage, downloader, ocr):
        self.repository, self.storage = repository, storage
        self.downloader, self.ocr = downloader, ocr

    @staticmethod
    def result(saved: SaveResult, user_id: int) -> MessageResult:
        if saved.code == 'duplicate_other_user':
            return MessageResult(saved.code, '该发票已被其他用户收录，未重复入库。', user_id)
        if saved.code == 'duplicate_same_user':
            return MessageResult(saved.code, '该发票已存在，未重复收录。\n发票号码：' + saved.invoice.invoice_number, user_id)
        if saved.code in ('binding_changed', 'message_owner_changed'):
            return MessageResult(saved.code, '账号关联已变更，请确认 Web 端绑定状态后重新发送。', user_id)
        invoice = saved.invoice
        if invoice.status == 'pending':
            return MessageResult('ocr_pending', '发票识别失败或信息不完整，原始 PDF 已保留，请在 Web 端补充信息。', user_id)
        return MessageResult('invoice_recorded',
                             f'已收录发票。\n抬头：{invoice.buyer_name}\n金额：{invoice.amount:.2f}\n发票号码：{invoice.invoice_number}',
                             user_id)

    def __call__(self, user_id: int, message: IncomingMessage) -> MessageResult:
        # Skip network calls and OCR billing on event redelivery or reply retry.
        existing = self.repository.find_message(user_id, message.message_id)
        if existing is not None:
            return self.result(existing, user_id)
        path = self.storage.invoice_path(user_id, message.message_id)
        if not path.is_file():
            try:
                content = self.downloader.download(message.message_id, message.file_key)
            except FileTooLargeError:
                return MessageResult('file_too_large', 'PDF 文件超过 10 MiB，请发送较小的文件。', user_id)
            except DownloadError:
                logger.error('Invoice download failed after retry limit')
                return MessageResult('download_failed', '文件下载失败，请稍后重新发送 PDF 发票。', user_id)
            try:
                path = self.storage.save_invoice(user_id, message.message_id, content)
            except InvalidPdfError:
                return MessageResult('invalid_pdf', '文件不是有效的 PDF，或已加密、无法读取，请重新发送。', user_id)
        # A crash after storing the original can resume without another download.
        data, error = recognize_or_pending(self.ocr, path)
        saved = self.repository.save(user_id, message.open_id, message.message_id, path, data, error)
        if saved.code in ('duplicate_same_user', 'duplicate_other_user'):
            # Never remove the original belonging to an existing database record.
            if saved.invoice is None or Path(saved.invoice.file_path) != path:
                path.unlink(missing_ok=True)
        return self.result(saved, user_id)


def build_ingestion(settings: Settings) -> InvoiceIngestion:
    return InvoiceIngestion(
        InvoiceRepository(settings.database_path),
        LocalStorage(settings.database_path.parent / 'invoices'),
        FeishuFileDownloader(build_api_client(settings)), AliyunInvoiceOcr(settings),
    )
