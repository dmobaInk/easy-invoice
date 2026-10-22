"""Render an authorized original PDF page to an image for in-page preview."""

from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import Response
import pymupdf


def preview_response(path: Path, page_number: int) -> Response:
    try:
        with pymupdf.open(path) as document:
            if not document.is_pdf or document.needs_pass or document.page_count < 1:
                raise HTTPException(422, '原件无法预览，请尝试下载原件')
            if page_number > document.page_count:
                raise HTTPException(404, '预览页码不存在')
            page = document.load_page(page_number - 1)
            longest = max(page.rect.width, page.rect.height)
            if longest <= 0:
                raise HTTPException(422, '原件页面尺寸无效')
            scale = min(2, 2400 / longest)
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            return Response(content=pixmap.tobytes('png'), media_type='image/png',
                            headers={'X-PDF-Page-Count': str(document.page_count),
                                     'Cache-Control': 'no-store'})
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(422, '原件预览失败，请尝试下载原件') from None
