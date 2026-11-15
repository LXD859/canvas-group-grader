from io import BytesIO

import pytest
from PIL import Image

from grader.preview import pdf_info, pdf_page
from grader.store import InputError


def test_render_synthetic_pdf(store, project):
    path, _ = store.file_path(project, project['files'][0]['id'])
    assert pdf_info(path)['pages'] == 1
    png = pdf_page(path, 0)
    with Image.open(BytesIO(png)) as image:
        assert image.width > 1000 and image.height > image.width
        assert image.width * image.height <= 1600 * 2200
    with pytest.raises(InputError):
        pdf_page(path, 1)
