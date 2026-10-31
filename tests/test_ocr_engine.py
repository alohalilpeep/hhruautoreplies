"""Независимые проверки приёма изображений, без подключения к hh."""
from io import BytesIO
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from ocr_engine import ImageCaptureError, capture_element, save_image


class ImageCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "images"
        self.image = Image.new("RGB", (24, 12), "red")
        self.addCleanup(self.image.close)

    def test_roundtrip_and_unique_names(self):
        first = save_image(self.image, self.directory)
        second = save_image(first, self.directory)
        third = save_image(first.read_bytes(), self.directory)
        self.assertEqual(len({first, second, third}), 3)
        for path in (first, second, third):
            with Image.open(path) as result:
                self.assertEqual(result.size, (24, 12))
                self.assertEqual(result.getpixel((0, 0)), (255, 0, 0))
        self.assertEqual(self.image.getpixel((0, 0)), (255, 0, 0))

    def test_missing_invalid_and_truncated(self):
        buffer = BytesIO()
        self.image.save(buffer, format="PNG")
        for source in (None, b"", b"not png", buffer.getvalue()[:40],
                       Path(self.temp.name) / "missing.png"):
            with self.subTest(source=repr(source)):
                with self.assertRaises(ImageCaptureError):
                    save_image(source, self.directory)
        self.assertFalse(self.directory.exists())

    def test_non_png_rejected(self):
        buffer = BytesIO()
        self.image.save(buffer, format="JPEG")
        with self.assertRaises(ImageCaptureError):
            save_image(buffer.getvalue(), self.directory)

    def test_output_is_file(self):
        first = save_image(self.image, self.directory)
        with self.assertRaises(ImageCaptureError):
            save_image(self.image, first)

    def test_capture_errors_and_empty_result(self):
        page = Mock()
        page.locator.return_value.screenshot.side_effect = RuntimeError("element missing")
        with self.assertRaises(ImageCaptureError):
            capture_element(page, "#missing", self.directory)
        page.locator.return_value.screenshot.side_effect = None
        page.locator.return_value.screenshot.return_value = b""
        with self.assertRaises(ImageCaptureError):
            capture_element(page, "#empty", self.directory)

    def test_capture_saves_png(self):
        buffer = BytesIO()
        self.image.save(buffer, format="PNG")
        page = Mock()
        page.locator.return_value.screenshot.return_value = buffer.getvalue()
        path = capture_element(page, "#sample", self.directory, timeout_ms=1500)
        page.locator.assert_called_once_with("#sample")
        page.locator.return_value.screenshot.assert_called_once_with(type="png", timeout=1500)
        with Image.open(path) as result:
            self.assertEqual(result.getpixel((0, 0)), (255, 0, 0))

    def test_invalid_timeout(self):
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.assertRaises(ImageCaptureError):
                capture_element(Mock(), "#sample", self.directory, timeout_ms=timeout)


if __name__ == "__main__":
    unittest.main()
