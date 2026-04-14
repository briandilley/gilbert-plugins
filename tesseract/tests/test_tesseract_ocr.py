"""Tests for the Tesseract OCR backend."""

from unittest.mock import MagicMock, patch

import pytest

from gilbert.interfaces.ocr import OCRBackend
from gilbert_plugin_tesseract.tesseract_ocr import TesseractOCR


# --- Backend registration ---


def test_tesseract_registered() -> None:
    backends = OCRBackend.registered_backends()
    assert "tesseract" in backends
    assert backends["tesseract"] is TesseractOCR


def test_backend_config_params() -> None:
    params = TesseractOCR.backend_config_params()
    keys = [p.key for p in params]
    assert "language" in keys


# --- TesseractOCR initialization ---


@pytest.fixture
def backend() -> TesseractOCR:
    return TesseractOCR()


async def test_initialize_available_when_deps_installed(backend: TesseractOCR) -> None:
    with patch.dict("sys.modules", {"pytesseract": MagicMock(), "PIL": MagicMock(), "PIL.Image": MagicMock()}):
        await backend.initialize({})
        assert backend.available is True


async def test_initialize_unavailable_when_deps_missing(backend: TesseractOCR) -> None:
    with patch("builtins.__import__", side_effect=ImportError("no pytesseract")):
        await backend.initialize({})
        assert backend.available is False


async def test_initialize_language_config(backend: TesseractOCR) -> None:
    with patch.dict("sys.modules", {"pytesseract": MagicMock(), "PIL": MagicMock(), "PIL.Image": MagicMock()}):
        await backend.initialize({"language": "eng+fra"})
        assert backend._language == "eng+fra"


async def test_initialize_default_language(backend: TesseractOCR) -> None:
    with patch.dict("sys.modules", {"pytesseract": MagicMock(), "PIL": MagicMock(), "PIL.Image": MagicMock()}):
        await backend.initialize({})
        assert backend._language == "eng"


async def test_close_sets_unavailable(backend: TesseractOCR) -> None:
    with patch.dict("sys.modules", {"pytesseract": MagicMock(), "PIL": MagicMock(), "PIL.Image": MagicMock()}):
        await backend.initialize({})
        assert backend.available is True
        await backend.close()
        assert backend.available is False


async def test_extract_text_returns_empty_when_unavailable(backend: TesseractOCR) -> None:
    result = await backend.extract_text(b"fake image data")
    assert result == ""
