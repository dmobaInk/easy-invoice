"""Store originals under user directories with server-generated file names."""

import hashlib
import os
from pathlib import Path
import tempfile

from app.integrations.feishu.files import MAX_PDF_BYTES


class InvalidPdfError(Exception):
    pass


def validate_pdf(data: bytes) -> int:
    import pymupdf

    if not data or len(data) > MAX_PDF_BYTES or b'%PDF-' not in data[:1024]:
        raise InvalidPdfError('invalid_pdf_header_or_size')
    try:
        with pymupdf.open(stream=data, filetype='pdf') as document:
            if not document.is_pdf or document.needs_pass or document.page_count < 1:
                raise InvalidPdfError('encrypted_or_empty_pdf')
            document.load_page(0)
            return document.page_count
    except InvalidPdfError:
        raise
    except Exception:
        raise InvalidPdfError('unreadable_pdf') from None


class LocalStorage:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def invoice_path(self, user_id: int, message_id: str) -> Path:
        if type(user_id) is not int or user_id <= 0:
            raise ValueError('Invalid user ID')
        name = hashlib.sha256(message_id.encode()).hexdigest() + '.pdf'
        return self.root / str(user_id) / name

    def save_invoice(self, user_id: int, message_id: str, data: bytes) -> Path:
        validate_pdf(data)
        target = self.invoice_path(user_id, message_id)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return target
