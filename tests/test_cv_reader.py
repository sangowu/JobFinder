"""测试 CV 文件读取。"""
from pathlib import Path

import pytest

from jobradar.cv_reader import read_cv

FIXTURE_DIR = Path(__file__).parent / "fixtures"


class TestReadCV:
    def test_read_md(self):
        text = read_cv(FIXTURE_DIR / "test_cv.md")
        assert "Zhang Wei" in text
        assert "Python" in text
        assert "London" in text

    def test_unsupported_format(self):
        import os
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"dummy")
            path = f.name
        try:
            with pytest.raises(ValueError, match="不支持的文件格式"):
                read_cv(path)
        finally:
            os.unlink(path)

    def test_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            read_cv("nonexistent.md")
