"""Независимые проверки polar unwrap на синтетическом изображении."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from PIL import Image, ImageDraw
from ocr_engine import ImageTransformError, polar_unwrap


class PolarUnwrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.image = Image.new("RGB", (100, 100), "white")
        draw = ImageDraw.Draw(self.image)
        draw.ellipse((20, 20, 80, 80), outline="black", width=5)
        self.before = self.image.tobytes()
        self.addCleanup(self.image.close)

    def test_quarter_circle_is_transposed_and_saved(self):
        result = polar_unwrap(self.image, (50, 50), 40, (0, 90), self.directory)
        self.assertEqual(result.image.shape, (40, 63, 3))
        self.assertTrue(result.original_path.is_file())
        self.assertTrue(result.unwrapped_path.is_file())
        self.assertNotEqual(result.original_path, result.unwrapped_path)
        self.assertIn("_original.png", result.original_path.name)
        self.assertIn("_unwrapped.png", result.unwrapped_path.name)
        self.assertEqual(self.image.tobytes(), self.before)
        with Image.open(result.original_path) as saved:
            self.assertEqual(saved.tobytes(), self.before)

    def test_angle_range_wraps_through_zero(self):
        result = polar_unwrap(self.image, (50, 50), 30, (300, 60), self.directory)
        expected_width = round((2 * np.pi * 30) * (120 / 360))
        self.assertEqual(result.image.shape, (30, expected_width, 3))

    def test_full_circle_and_unique_names(self):
        first = polar_unwrap(self.image, (50, 50), 25, (0, 360), self.directory)
        second = polar_unwrap(self.image, (50, 50), 25, (-180, 180), self.directory)
        self.assertEqual(first.image.shape, (25, 158, 3))
        self.assertNotEqual(first.original_path, second.original_path)

    def test_horizontal_flip_reverses_angular_axis(self):
        normal = polar_unwrap(self.image, (50, 50), 30, (0, 90), self.directory)
        flipped = polar_unwrap(
            self.image, (50, 50), 30, (0, 90), self.directory,
            flip_horizontal=True)
        self.assertTrue(np.array_equal(flipped.image, normal.image[:, ::-1]))

    def test_invalid_parameters_leave_no_files(self):
        cases = (
            {"center": (float("nan"), 50), "radius": 20, "angle_range": (0, 90)},
            {"center": (50,), "radius": 20, "angle_range": (0, 90)},
            {"center": (50, 50), "radius": 0, "angle_range": (0, 90)},
            {"center": (50, 50), "radius": 20, "angle_range": (90, 90)},
            {"center": (50, 50), "radius": 20, "angle_range": (0, 500)},
            {"center": (50, 50), "radius": 20, "angle_range": (0, 90),
             "flip_horizontal": "yes"},
        )
        for params in cases:
            with self.subTest(params=params):
                with self.assertRaises(ImageTransformError):
                    polar_unwrap(self.image, output_dir=self.directory, **params)
        self.assertEqual(list(self.directory.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
