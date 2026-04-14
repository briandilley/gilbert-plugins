"""Tesseract OCR plugin — registers the TesseractOCR backend with the OCR service."""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class TesseractPlugin(Plugin):
    """Side-effect plugin: importing ``tesseract_ocr`` registers the backend."""

    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="tesseract",
            version="1.0.0",
            description="Tesseract OCR backend (local, pytesseract-based)",
            provides=["tesseract_ocr"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        # Importing the module triggers OCRBackend.__init_subclass__,
        # registering "tesseract" in the backend registry.
        from . import tesseract_ocr  # noqa: F401

    async def teardown(self) -> None:
        pass


def create_plugin() -> Plugin:
    return TesseractPlugin()
