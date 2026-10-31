"""Пассивный сбор снимков капчи без запуска браузера."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hh_autoapply as hh


class CaptchaCaptureTests(unittest.TestCase):
    def setUp(self):
        hh._CAPTCHA_CAPTURED_PAGES.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def test_prefers_visible_captcha_element(self):
        hidden = Mock()
        hidden.is_visible.return_value = False
        visible = Mock()
        visible.is_visible.return_value = True

        def save(**kwargs):
            Path(kwargs["path"]).write_bytes(b"png")

        visible.screenshot.side_effect = save
        collection = Mock()
        collection.count.return_value = 2
        collection.nth.side_effect = [hidden, visible]
        page = Mock()
        page.locator.return_value = collection
        with patch.object(hh, "CAPTCHA_IMAGE_DIR", self.directory):
            path = hh.capture_captcha(page)
        self.assertTrue(path.is_file())
        visible.screenshot.assert_called_once()
        page.screenshot.assert_not_called()

    def test_falls_back_to_page_screenshot(self):
        collection = Mock()
        collection.count.return_value = 0
        page = Mock()
        page.locator.return_value = collection
        page.screenshot.side_effect = lambda **kw: Path(kw["path"]).write_bytes(b"png")
        with patch.object(hh, "CAPTCHA_IMAGE_DIR", self.directory):
            path = hh.capture_captcha(page)
        self.assertTrue(path.is_file())
        page.screenshot.assert_called_once()

    def test_one_capture_per_continuous_encounter(self):
        page = Mock(url="https://hh.ru/captcha")
        with patch.object(hh, "visible", return_value=False), \
             patch.object(hh, "visible_text", return_value=False), \
             patch.object(hh, "capture_captcha", return_value=self.directory / "one.png") as capture:
            self.assertTrue(hh.captcha_present(page))
            self.assertTrue(hh.captcha_present(page))
            capture.assert_called_once_with(page)
            page.url = "https://hh.ru/search"
            self.assertFalse(hh.captcha_present(page))
            page.url = "https://hh.ru/captcha"
            self.assertTrue(hh.captcha_present(page))
            self.assertEqual(capture.call_count, 2)

    def test_capture_error_does_not_hide_captcha(self):
        page = Mock(url="https://hh.ru/captcha")
        with patch.object(hh, "capture_captcha", side_effect=RuntimeError("disk")):
            self.assertTrue(hh.captcha_present(page))


if __name__ == "__main__":
    unittest.main()
