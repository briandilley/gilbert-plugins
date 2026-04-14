"""Tesseract OCR backend — text extraction via pytesseract."""

import asyncio
import io
import logging
from typing import Any

from gilbert.interfaces.ocr import OCRBackend

logger = logging.getLogger(__name__)


class TesseractOCR(OCRBackend):
    """OCR backend using Tesseract via pytesseract.

    Gracefully degrades if pytesseract or Pillow are not installed.
    """

    backend_name = "tesseract"

    @classmethod
    def backend_config_params(cls) -> list["ConfigParam"]:
        from gilbert.interfaces.configuration import ConfigParam
        from gilbert.interfaces.tools import ToolParameterType

        return [
            ConfigParam(
                key="language",
                type=ToolParameterType.STRING,
                description="Tesseract language code (e.g., 'eng', 'eng+fra').",
                default="eng",
            ),
        ]

    def __init__(self) -> None:
        self._available = False
        self._language: str = "eng"

    async def initialize(self, config: dict[str, Any]) -> None:
        self._language = str(config.get("language", "eng"))

        try:
            import pytesseract  # noqa: F401
            from PIL import Image  # noqa: F401

            self._available = True
            logger.info("Tesseract OCR backend initialized (language=%s)", self._language)
        except ImportError:
            self._available = False
            logger.warning(
                "Tesseract OCR backend: pytesseract or Pillow not installed — OCR disabled"
            )

    async def close(self) -> None:
        self._available = False

    @property
    def available(self) -> bool:
        return self._available

    async def extract_text(self, image_bytes: bytes) -> str:
        if not self._available:
            return ""

        try:
            import pytesseract
            from PIL import Image

            lang = self._language

            def _ocr() -> str:
                img = Image.open(io.BytesIO(image_bytes))
                return pytesseract.image_to_string(img, lang=lang)

            result = await asyncio.to_thread(_ocr)
            return result.strip()

        except Exception:
            logger.warning("Tesseract OCR extraction failed", exc_info=True)
            return ""
