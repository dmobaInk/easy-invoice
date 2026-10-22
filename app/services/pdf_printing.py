"""Render only the first PDF page at print resolution into A4 half-page slots."""

from io import BytesIO
import os
from pathlib import Path
import tempfile

import pymupdf
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas


class PdfGenerationError(Exception):
    pass


def generate_a4(paths: list[Path], target: Path) -> None:
    if not paths:
        raise PdfGenerationError('请选择需要打印的发票')
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        fd, name = tempfile.mkstemp(dir=target.parent, suffix='.tmp')
        os.close(fd)
        temporary = Path(name)
        width, height = A4
        margin, gap = 18, 12
        slot_width, slot_height = width - 2 * margin, height / 2 - margin - gap / 2
        output = canvas.Canvas(str(temporary), pagesize=A4, pageCompression=1)
        output.setTitle('Invoice A4 print')
        for index, path in enumerate(paths):
            with pymupdf.open(path) as document:
                if document.needs_pass or not document.is_pdf or document.page_count < 1:
                    raise PdfGenerationError('存在无法读取或已加密的原始 PDF')
                page = document.load_page(0)
                scale = min(200 / 72, 4096 / max(page.rect.width, page.rect.height))
                pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
                image = ImageReader(BytesIO(pixmap.tobytes('png')))
                ratio = min(slot_width / pixmap.width, slot_height / pixmap.height)
                draw_width, draw_height = pixmap.width * ratio, pixmap.height * ratio
                bottom = height / 2 + gap / 2 if index % 2 == 0 else margin
                output.drawImage(image, (width - draw_width) / 2,
                                 bottom + (slot_height - draw_height) / 2,
                                 width=draw_width, height=draw_height, mask='auto')
            if index % 2 == 1 or index == len(paths) - 1:
                output.showPage()
        output.save()
        with pymupdf.open(temporary) as verification:
            if verification.page_count != (len(paths) + 1) // 2:
                raise PdfGenerationError('打印文件页数校验失败')
        with temporary.open('rb') as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except PdfGenerationError:
        raise
    except Exception:
        raise PdfGenerationError('PDF 生成失败，请检查原始文件后重试') from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
