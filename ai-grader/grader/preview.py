import io
from contextlib import closing

import pypdfium2 as pdfium

from .store import InputError


def pdf_info(path):
    try:
        with closing(pdfium.PdfDocument(path)) as doc:
            return {'pages': len(doc)}
    except Exception:
        raise InputError('无法打开该 PDF，可能已加密或损坏。可下载原件核对。') from None


def pdf_page(path, index):
    try:
        with closing(pdfium.PdfDocument(path)) as doc:
            if index < 0 or index >= len(doc):
                raise InputError('页码超出范围。')
            with closing(doc[index]) as page:
                width, height = page.get_size()
                scale = min(2.5, 1600 / max(width, 1), 2200 / max(height, 1))
                bitmap = page.render(scale=scale)
                try:
                    picture = bitmap.to_pil()
                    try:
                        out = io.BytesIO()
                        picture.save(out, format='PNG')
                        return out.getvalue()
                    finally:
                        picture.close()
                finally:
                    bitmap.close()
    except InputError:
        raise
    except Exception:
        raise InputError('PDF 页面预览失败，可下载原件核对。') from None
