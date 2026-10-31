"""OCR обычного синтетического текста; требуется установленный Tesseract 5."""
from io import BytesIO
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import ocr_engine
from ocr_engine import OCRError, PolarUnwrapResult, _tesseract_version, recognize


class RecognitionTests(unittest.TestCase):
    def setUp(self):
        self.image = Image.new("RGB", (600, 100), "white")
        ImageDraw.Draw(self.image).text(
            (20, 20), "Hello world 123", font=ImageFont.load_default(size=40), fill="black")
        self.addCleanup(self.image.close)

    def test_real_ocr_both_modes(self):
        for preprocess in (True, False):
            with self.subTest(preprocess=preprocess):
                result = recognize(self.image, preprocess=preprocess, psm=7)
                self.assertEqual(result.text.strip(), "Hello world 123")
                self.assertGreater(result.recognition_seconds, 0)
                self.assertGreaterEqual(result.total_seconds,
                                        result.loading_seconds + result.recognition_seconds
                                        + result.preprocessing_seconds)
                if not preprocess:
                    self.assertEqual(result.preprocessing_seconds, 0)
                print(f"preprocess={preprocess}: {result}")

    def test_png_bytes(self):
        buf = BytesIO()
        self.image.save(buf, format="PNG")
        self.assertEqual(recognize(buf.getvalue(), psm=7).text.strip(), "Hello world 123")

    def test_missing_language(self):
        with self.assertRaises(OCRError):
            recognize(self.image, lang="nonexistent_language_for_test")

    def test_invalid_image(self):
        for source in (None, b"broken", Path("missing_ocr_test_input.png")):
            with self.assertRaises(OCRError):
                recognize(source)

    def test_timeout_and_wrong_version(self):
        with patch("pytesseract.image_to_string", side_effect=RuntimeError("Tesseract process timeout")):
            with self.assertRaisesRegex(OCRError, "timeout"):
                recognize(self.image)
        _tesseract_version.cache_clear()
        with patch("pytesseract.get_tesseract_version", return_value="4.1.0"):
            with self.assertRaisesRegex(OCRError, "Требуется Tesseract 5"):
                recognize(self.image)
        _tesseract_version.cache_clear()

    def test_options_and_no_preprocessing(self):
        with patch("pytesseract.image_to_string", return_value="text") as engine:
            with patch("cv2.threshold", side_effect=AssertionError("preprocessing called")):
                recognize(self.image, lang="rus+eng", preprocess=False,
                          psm=7, oem=1, config="-c preserve_interword_spaces=1", timeout=2)
            self.assertEqual(engine.call_args.kwargs, {
                "lang": "rus+eng", "config": "--psm 7 --oem 1 -c preserve_interword_spaces=1",
                "timeout": 2})

    def test_filters_are_independently_configurable(self):
        prepared = object()
        with patch("ocr_engine._apply_filters", return_value=prepared) as filters, \
             patch("pytesseract.image_to_string", return_value="text") as engine:
            recognize(self.image, grayscale=False, clahe=True,
                      gaussian_blur=True, otsu=False)
        filters.assert_called_once()
        self.assertEqual(filters.call_args.kwargs, {
            "grayscale": False, "clahe": True,
            "gaussian_blur": True, "otsu": False})
        self.assertIs(engine.call_args.args[0], prepared)

    def test_polar_pipeline_preprocesses_once_and_compares_psm(self):
        pixels = np.zeros((20, 100, 3), dtype=np.uint8)
        unwrap = PolarUnwrapResult(pixels, Path("original.png"), Path("unwrapped.png"))
        prepared = np.zeros((20, 100), dtype=np.uint8)
        fake_result = Mock()
        with patch("ocr_engine.polar_unwrap", return_value=unwrap), \
             patch("ocr_engine._apply_filters", return_value=prepared) as filters, \
             patch("ocr_engine.recognize", return_value=fake_result) as engine:
            result = ocr_engine.recognize_polar(
                "input.png", (10, 10), 20, (0, 90), lang="rus+eng",
                grayscale=True, clahe=False, gaussian_blur=True, otsu=False,
                radial_range=(5, 15), padding=2)
        filters.assert_called_once_with(
            pixels, grayscale=True, clahe=False, gaussian_blur=True, otsu=False)
        self.assertEqual([call.kwargs["psm"] for call in engine.call_args_list], [7, 13])
        self.assertTrue(all(call.kwargs["preprocess"] is False
                            for call in engine.call_args_list))
        self.assertTrue(all(call.args[0].shape == (14, 104)
                            for call in engine.call_args_list))
        self.assertEqual(result.results, {7: fake_result, 13: fake_result})

    def test_path_without_preprocessing_goes_directly_to_tesseract(self):
        path = Path(self._testMethodName + ".png")
        self.image.save(path)
        self.addCleanup(path.unlink, missing_ok=True)
        with patch("pytesseract.image_to_string", return_value="text") as engine:
            recognize(path, preprocess=False)
        self.assertEqual(engine.call_args.args[0], str(path))

    def test_statistics_csv(self):
        csv_path = Path(self._testMethodName + ".csv")
        self.addCleanup(csv_path.unlink, missing_ok=True)
        with patch("pytesseract.image_to_string", return_value="text"):
            recognize(self.image, preprocess=False, stats_csv=csv_path)
            recognize(self.image, preprocess=True, stats_csv=csv_path)
        rows = csv_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(rows), 3)
        self.assertIn("loading_seconds", rows[0])
        self.assertIn("recognition_seconds", rows[0])
        self.assertIn(",False,", rows[1])
        self.assertIn(",True,", rows[2])

    def test_version_check_is_cached(self):
        _tesseract_version.cache_clear()
        with patch("pytesseract.get_tesseract_version", return_value="5.5.4") as version:
            with patch("pytesseract.image_to_string", return_value="text"):
                recognize(self.image, preprocess=False)
                recognize(self.image, preprocess=False)
        version.assert_called_once_with()

    def test_invalid_options(self):
        for options in ({"psm": 99}, {"oem": -1}, {"lang": ""},
                        {"timeout": 0}, {"timeout": float("nan")}, {"preprocess": "false"}):
            with self.assertRaises(OCRError):
                recognize(self.image, **options)


if __name__ == "__main__":
    unittest.main()
