"""Download message attachments using message resource API, not drive files API."""

import logging
import time

MAX_PDF_BYTES = 10 * 1024 * 1024
logger = logging.getLogger(__name__)


class DownloadError(Exception):
    pass


class FileTooLargeError(DownloadError):
    pass


class FeishuFileDownloader:
    def __init__(self, client, *, sleep=time.sleep):
        self.client, self.sleep = client, sleep

    def download(self, message_id: str, file_key: str) -> bytes:
        from lark_oapi.api.im.v1 import GetMessageResourceRequest

        request = (GetMessageResourceRequest.builder().message_id(message_id)
                   .file_key(file_key).type('file').build())
        for attempt in range(3):
            stream = None
            try:
                response = self.client.im.v1.message_resource.get(request)
                stream = response.file
                if not response.success() or stream is None:
                    raise DownloadError('feishu_resource_rejected')
                data = stream.read(MAX_PDF_BYTES + 1)
                if len(data) > MAX_PDF_BYTES:
                    raise FileTooLargeError('pdf_exceeds_10_mib')
                if not data:
                    raise DownloadError('empty_download')
                return data
            except FileTooLargeError:
                raise
            except Exception:
                logger.warning('Feishu file download failed: attempt=%d', attempt + 1)
                if attempt == 2:
                    raise DownloadError('feishu_download_failed') from None
                self.sleep(2 ** attempt)
            finally:
                if stream is not None:
                    stream.close()
        raise AssertionError('unreachable')
