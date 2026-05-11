import unittest

from app.services.pdf_export import PdfExportService


class PdfExportTests(unittest.TestCase):
    def test_build_catalog_pdf_returns_pdf_bytes(self) -> None:
        payload = PdfExportService.build_catalog_pdf()
        self.assertIsInstance(payload, bytes)
        self.assertGreater(len(payload), 100)
        self.assertTrue(payload.startswith(b"%PDF"))


if __name__ == "__main__":
    unittest.main()