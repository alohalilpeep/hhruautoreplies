"""Захват изображений и OCR, без подключения к логике откликов.

save_image принимает PNG (путь или bytes) либо PIL.Image.Image.
capture_element работает с синхронной страницей Playwright, предоставленной
вызывающим кодом. recognize() выполняет OCR через Tesseract 5.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from time import perf_counter
from io import BytesIO
from pathlib import Path
from uuid import uuid4
import math
import os
import csv

from PIL import Image


DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "ocr_images"
DEFAULT_POLAR_OUTPUT_DIR = Path(__file__).resolve().parent / "polar_images"

# Проверенный пресет для captcha_images/test.png. Словарь можно передать в
# recognize_polar через **CURVED_TEXT_PRESET; эталон: «толокняная млея».
CURVED_TEXT_PRESET = {
    "center": (330, -20),
    "radius": 245,
    "angle_range": (12, 170),
    "flip_horizontal": True,
    "grayscale": True,
    "clahe": True,
    "gaussian_blur": False,
    "otsu": True,
    "radial_range": (155, None),
    "padding": 25,
}


class ImageCaptureError(Exception):
    """Изображение отсутствует, повреждено или не удалось его сохранить."""


class ImageTransformError(Exception):
    """Некорректное изображение или параметры геометрического преобразования."""


@dataclass(frozen=True)
class PolarUnwrapResult:
    image: object
    original_path: Path
    unwrapped_path: Path


@dataclass(frozen=True)
class PolarOCRResult:
    unwrap: PolarUnwrapResult
    results: dict
    preprocessing_seconds: float


def save_image(source, output_dir=DEFAULT_OUTPUT_DIR):
    """Сохранить независимую копию изображения как PNG и вернуть Path.

    Путь и bytes должны содержать PNG; объект Pillow может иметь любой
    формат. Изображение вызывающего кода не изменяется и не закрывается.
    """
    image = None
    try:
        if isinstance(source, Image.Image):
            source.load()
            image = source.copy()
        elif isinstance(source, (str, os.PathLike, bytes, bytearray)):
            incoming = BytesIO(source) if isinstance(source, (bytes, bytearray)) else source
            with Image.open(incoming) as opened:
                if opened.format != "PNG":
                    raise ImageCaptureError("Ожидалось изображение в формате PNG")
                opened.load()
                image = opened.copy()
        else:
            raise ImageCaptureError("Передай путь к PNG, PNG-байты или объект Pillow Image")
        if image.width <= 0 or image.height <= 0:
            raise ImageCaptureError("Изображение имеет нулевой размер")
        if image.mode not in ("1", "L", "LA", "I", "I;16", "P", "RGB", "RGBA"):
            converted = image.convert("RGBA")
            image.close()
            image = converted
        # Кодируем до создания файла, чтобы ошибки изображения не оставляли
        # пустой артефакт. Режим xb защищает существующие файлы от перезаписи.
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        directory = Path(output_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"image_{uuid4().hex}.png"
        with path.open("xb") as target:
            try:
                target.write(buffer.getvalue())
            except OSError:
                path.unlink(missing_ok=True)
                raise
        return path
    except ImageCaptureError:
        raise
    except (OSError, ValueError, TypeError, Image.DecompressionBombError) as exc:
        raise ImageCaptureError(f"Не удалось прочитать или сохранить изображение: {exc}") from exc
    finally:
        if image is not None:
            image.close()


def capture_element(page, selector, output_dir=DEFAULT_OUTPUT_DIR, *, timeout_ms=5000):
    """Снять один видимый DOM-элемент на независимой тестовой странице.

    selector должен однозначно определять элемент. Ожидание появления и
    видимости ограничено timeout_ms; ноль (бесконечное ожидание) запрещён.
    Возвращает путь к сохранённому PNG или выбрасывает ImageCaptureError.
    """
    if not isinstance(selector, str) or not selector.strip():
        raise ImageCaptureError("Нужен непустой селектор DOM-элемента")
    if not isinstance(timeout_ms, (int, float)) or not math.isfinite(timeout_ms) or timeout_ms <= 0:
        raise ImageCaptureError("timeout_ms должен быть конечным положительным числом")
    try:
        data = page.locator(selector).screenshot(type="png", timeout=timeout_ms)
    except Exception as exc:
        raise ImageCaptureError(f"Не удалось снять DOM-элемент {selector!r}: {exc}") from exc
    return save_image(data, output_dir)


def polar_unwrap(image, center, radius, angle_range=(0, 360),
                 output_dir=DEFAULT_POLAR_OUTPUT_DIR, *, flip_horizontal=False):
    """Развернуть кольцевой фрагмент в прямоугольник через cv2.warpPolar.

    center — (x, y) в пикселях, radius — внешний радиус. angle_range —
    (start, end) в градусах с положительным ходом и переходом через 360:
    (300, 60) означает сектор 120°. В результате угол идёт горизонтально,
    радиус — вертикально. flip_horizontal меняет направление угловой оси.
    Исходное изображение не изменяется.

    Возвращает PolarUnwrapResult с новым numpy-массивом и путями к паре PNG.
    """
    original_path = unwrapped_path = None
    try:
        import cv2
        import numpy as np

        if (not isinstance(center, (tuple, list)) or len(center) != 2
                or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in center)):
            raise ImageTransformError("center должен быть парой конечных координат (x, y)")
        if not isinstance(radius, (int, float)) or not math.isfinite(radius) or radius <= 0:
            raise ImageTransformError("radius должен быть конечным положительным числом")
        if (not isinstance(angle_range, (tuple, list)) or len(angle_range) != 2
                or not all(isinstance(v, (int, float)) and math.isfinite(v)
                           for v in angle_range)):
            raise ImageTransformError("angle_range должен быть парой углов в градусах")
        if type(flip_horizontal) is not bool:
            raise ImageTransformError("flip_horizontal должен иметь тип bool")
        start_angle, end_angle = angle_range
        raw_span = end_angle - start_angle
        span = raw_span % 360
        if math.isclose(span, 0.0):
            if math.isclose(raw_span, 0.0):
                raise ImageTransformError("угловой диапазон не может быть нулевым")
            span = 360.0
        if abs(raw_span) > 360:
            raise ImageTransformError("угловой диапазон не может превышать 360 градусов")

        # save_image работает с копией и одновременно проверяет PNG-вход.
        directory = Path(output_dir)
        temporary_original = save_image(image, directory)
        token = uuid4().hex
        original_path = directory / f"polar_{token}_original.png"
        unwrapped_path = directory / f"polar_{token}_unwrapped.png"
        temporary_original.replace(original_path)

        with Image.open(original_path) as opened:
            opened.load()
            x, y = float(center[0]), float(center[1])
            # Центр дуги может находиться за пределами обрезанного кадра;
            # отсутствующую область warpPolar заполнит через FILL_OUTLIERS.
            # OpenCV не композитит альфа-канал; прозрачность кладём на белый фон.
            with opened.convert("RGBA") as rgba:
                with Image.new("RGBA", rgba.size, "white") as background:
                    background.alpha_composite(rgba)
                    with background.convert("RGB") as rgb:
                        source = np.asarray(rgb).copy()

        polar_height = max(1, math.ceil(2 * math.pi * radius))
        polar_width = max(1, math.ceil(radius))
        polar = cv2.warpPolar(
            source, (polar_width, polar_height), (x, y), float(radius),
            cv2.WARP_POLAR_LINEAR | cv2.WARP_FILL_OUTLIERS | cv2.INTER_CUBIC,
        )
        start_row = round((start_angle % 360) / 360 * polar_height)
        row_count = max(1, round(span / 360 * polar_height))
        rows = (start_row + np.arange(row_count)) % polar_height
        sector = np.take(polar, rows, axis=0)
        # warpPolar: строки — угол, столбцы — радиус. После transpose текст
        # вдоль дуги становится горизонтальным, центр находится сверху.
        unwrapped = np.ascontiguousarray(np.swapaxes(sector, 0, 1))
        if flip_horizontal:
            unwrapped = np.ascontiguousarray(np.flip(unwrapped, axis=1))
        Image.fromarray(unwrapped).save(unwrapped_path, format="PNG")
        return PolarUnwrapResult(unwrapped, original_path, unwrapped_path)
    except ImageTransformError:
        for path in (original_path, unwrapped_path):
            if path is not None:
                path.unlink(missing_ok=True)
        raise
    except ImportError as exc:
        raise ImageTransformError(
            "Установи зависимости: pip install -r requirements-ocr.txt") from exc
    except Exception as exc:
        for path in (original_path, unwrapped_path):
            if path is not None:
                path.unlink(missing_ok=True)
        raise ImageTransformError(f"Не удалось выполнить polar unwrap: {exc}") from exc


class OCRError(Exception):
    """Ошибка настроек, входного изображения или движка OCR."""


@dataclass(frozen=True)
class OCRResult:
    text: str
    language: str
    loading_seconds: float
    preprocessing_seconds: float
    recognition_seconds: float
    total_seconds: float


@lru_cache(maxsize=1)
def _tesseract_version():
    """Проверить движок один раз за процесс, без повторного subprocess."""
    import pytesseract

    version = pytesseract.get_tesseract_version()
    if str(version).split(".")[0] != "5":
        raise OCRError(f"Требуется Tesseract 5, установлен {version}")
    return str(version)


def _write_statistics(path, result, *, source, preprocess, grayscale, clahe,
                      gaussian_blur, otsu, psm, oem, config):
    """Добавить один законченный замер в CSV."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fields = (
        "timestamp_utc", "source", "language", "preprocess", "grayscale", "clahe",
        "gaussian_blur", "otsu", "psm", "oem", "config",
        "loading_seconds", "preprocessing_seconds", "recognition_seconds",
        "total_seconds", "characters",
    )
    new_file = not target.exists() or target.stat().st_size == 0
    with target.open("a", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        if new_file:
            writer.writeheader()
        writer.writerow({
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "language": result.language,
            "preprocess": preprocess,
            "grayscale": grayscale,
            "clahe": clahe,
            "gaussian_blur": gaussian_blur,
            "otsu": otsu,
            "psm": psm,
            "oem": oem,
            "config": config,
            "loading_seconds": f"{result.loading_seconds:.9f}",
            "preprocessing_seconds": f"{result.preprocessing_seconds:.9f}",
            "recognition_seconds": f"{result.recognition_seconds:.9f}",
            "total_seconds": f"{result.total_seconds:.9f}",
            "characters": len(result.text),
        })


def _apply_filters(rgb, *, grayscale=True, clahe=False, gaussian_blur=False,
                   otsu=True):
    """Применить только явно включённые OpenCV-фильтры к RGB-массиву."""
    import cv2

    output = rgb
    if grayscale:
        output = cv2.cvtColor(output, cv2.COLOR_RGB2GRAY)
    if clahe:
        if output.ndim == 2:
            output = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(output)
        else:
            lab = cv2.cvtColor(output, cv2.COLOR_RGB2LAB)
            light, a, b = cv2.split(lab)
            light = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(light)
            output = cv2.cvtColor(cv2.merge((light, a, b)), cv2.COLOR_LAB2RGB)
    if gaussian_blur:
        output = cv2.GaussianBlur(output, (3, 3), 0)
    if otsu:
        if output.ndim == 3:
            output = cv2.cvtColor(output, cv2.COLOR_RGB2GRAY)
        _, output = cv2.threshold(output, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return output


def recognize(source, *, lang="eng", preprocess=True, grayscale=True,
              clahe=False, gaussian_blur=False, otsu=True, psm=6, oem=3,
              config="", timeout=30, stats_csv=None):
    """Распознать PNG (путь/bytes) или Pillow Image.

    Фильтры grayscale, CLAHE, Gaussian Blur и Otsu включаются независимо.
    preprocess=False отключает их все без импорта и вызова OpenCV.
    config передаёт дополнительные параметры Tesseract, например
    '-c preserve_interword_spaces=1'. timeout ограничивает сам OCR.
    stats_csv добавляет замер в CSV. Время total включает загрузку и все стадии;
    recognition включает запуск процесса и обмен данными с Tesseract.
    """
    start = perf_counter()
    if not isinstance(lang, str) or not lang.strip():
        raise OCRError("Укажи язык, например eng, rus или rus+eng")
    if type(psm) is not int or psm not in range(3, 14):
        raise OCRError("psm должен быть целым числом от 3 до 13 для текстового OCR")
    if type(oem) is not int or oem not in range(4):
        raise OCRError("oem должен быть целым числом от 0 до 3")
    filters = (grayscale, clahe, gaussian_blur, otsu)
    if (not isinstance(config, str) or type(preprocess) is not bool
            or any(type(value) is not bool for value in filters)):
        raise OCRError("config должен быть строкой, параметры обработки — bool")
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise OCRError("timeout должен быть конечным положительным числом")
    image = None
    owns_image = False
    try:
        import pytesseract

        _tesseract_version()
        loading_start = perf_counter()
        source_name = "memory"
        if isinstance(source, Image.Image):
            source.load()
            image = source
        elif isinstance(source, (str, os.PathLike)):
            path = Path(source)
            source_name = path.name
            image = Image.open(path)
            owns_image = True
            if image.format != "PNG":
                raise OCRError("Ожидалось изображение в формате PNG")
            image.verify()
            image.close()
            image = None
            # Без OpenCV Tesseract читает PNG сам: исключаем декодирование и
            # повторное кодирование в Pillow/pytesseract.
            if preprocess and any(filters):
                image = Image.open(path)
                owns_image = True
                image.load()
            else:
                payload = str(path)
        elif isinstance(source, (bytes, bytearray)):
            image = Image.open(BytesIO(source))
            owns_image = True
            if image.format != "PNG":
                raise OCRError("Ожидалось изображение в формате PNG")
            image.load()
        else:
            try:
                import numpy as np
                is_array = isinstance(source, np.ndarray)
            except ImportError:
                is_array = False
            if not is_array or source.dtype != np.uint8 or source.ndim not in (2, 3):
                raise OCRError(
                    "Передай путь к PNG, PNG-байты, Pillow Image или uint8 numpy-массив")
            if source.size == 0 or (source.ndim == 3 and source.shape[2] not in (3, 4)):
                raise OCRError("Некорректный размер numpy-изображения")
            payload = source
        if image is not None and (image.width <= 0 or image.height <= 0):
            raise OCRError("Изображение имеет нулевой размер")
        loading = perf_counter() - loading_start
        preparation = 0.0
        if image is not None:
            payload = image
        if preprocess and any(filters):
            import numpy as np

            stage = perf_counter()
            if image is not None:
                # Прозрачный фон превращаем в белый перед фильтрами.
                with image.convert("RGBA") as rgba:
                    with Image.new("RGBA", rgba.size, "white") as background:
                        background.alpha_composite(rgba)
                        with background.convert("RGB") as rgb:
                            rgb_array = np.asarray(rgb)
            else:
                rgb_array = payload
                if rgb_array.ndim == 2:
                    rgb_array = np.repeat(rgb_array[:, :, None], 3, axis=2)
                elif rgb_array.shape[2] == 4:
                    alpha = rgb_array[:, :, 3:4].astype(np.float32) / 255
                    rgb_array = (rgb_array[:, :, :3] * alpha + 255 * (1 - alpha)).astype(np.uint8)
            payload = _apply_filters(
                rgb_array, grayscale=grayscale, clahe=clahe,
                gaussian_blur=gaussian_blur, otsu=otsu)
            preparation = perf_counter() - stage
        stage = perf_counter()
        text = pytesseract.image_to_string(
            payload, lang=lang, config=f"--psm {psm} --oem {oem} {config}".strip(),
            timeout=timeout)
        elapsed = perf_counter() - stage
        result = OCRResult(text, lang, loading, preparation, elapsed, perf_counter() - start)
        if stats_csv is not None:
            _write_statistics(
                stats_csv, result, source=source_name, preprocess=preprocess,
                grayscale=grayscale, clahe=clahe, gaussian_blur=gaussian_blur,
                otsu=otsu, psm=psm, oem=oem, config=config)
        return result
    except OCRError:
        raise
    except ImportError as exc:
        raise OCRError("Установи зависимости: pip install -r requirements-ocr.txt") from exc
    except Exception as exc:
        raise OCRError(f"Ошибка OCR: {exc}") from exc
    finally:
        if image is not None and owns_image:
            image.close()


def recognize_polar(source, center, radius, angle_range, *, lang="eng", oem=3,
                    config="", timeout=30, grayscale=True, clahe=False,
                    gaussian_blur=False, otsu=True,
                    output_dir=DEFAULT_POLAR_OUTPUT_DIR, flip_horizontal=False,
                    radial_range=None, padding=0):
    """Выпрямить изображение и сравнить существующий OCR в PSM 7 и 13.

    OpenCV-фильтры применяются к развертке один раз. Оба запуска используют
    recognize(preprocess=False), поэтому повторной обработки изображения нет.
    """
    flags = (grayscale, clahe, gaussian_blur, otsu)
    if any(type(value) is not bool for value in flags):
        raise OCRError("параметры обработки должны иметь тип bool")
    if type(padding) is not int or padding < 0:
        raise OCRError("padding должен быть целым неотрицательным числом")
    if radial_range is not None:
        if (not isinstance(radial_range, (tuple, list)) or len(radial_range) != 2
                or any(value is not None and type(value) is not int
                       for value in radial_range)):
            raise OCRError("radial_range должен быть парой целых индексов или None")
    unwrap = polar_unwrap(source, center, radius, angle_range, output_dir,
                          flip_horizontal=flip_horizontal)
    stage = perf_counter()
    prepared = unwrap.image
    if any(flags):
        prepared = _apply_filters(
            prepared, grayscale=grayscale, clahe=clahe,
            gaussian_blur=gaussian_blur, otsu=otsu)
    if radial_range is not None:
        start, end = radial_range
        prepared = prepared[slice(start, end), :]
        if prepared.size == 0:
            raise OCRError("radial_range оставил пустое изображение")
    if padding:
        import cv2

        # После Otsu фон белый; для цветного/серого изображения белое поле
        # также является нейтральным и не создаёт новых границ для OCR.
        prepared = cv2.copyMakeBorder(
            prepared, padding, padding, padding, padding,
            cv2.BORDER_CONSTANT,
            value=(255, 255, 255) if prepared.ndim == 3 else 255)
    preprocessing_seconds = perf_counter() - stage
    results = {
        psm: recognize(prepared, lang=lang, preprocess=False, psm=psm, oem=oem,
                       config=config, timeout=timeout)
        for psm in (7, 13)
    }
    return PolarOCRResult(unwrap, results, preprocessing_seconds)
