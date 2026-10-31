# Захват изображений

Установка: `venv/bin/python -m pip install -r requirements-ocr.txt`.
Модуль принимает и сохраняет изображения и распознаёт текст через Tesseract 5.

```python
from PIL import Image
from ocr_engine import save_image, capture_element, ImageCaptureError

saved = save_image("example.png")
saved = save_image(Image.new("RGB", (200, 60), "white"))
saved = save_image(saved.read_bytes())

# page — синхронная страница Playwright с независимым тестовым содержимым.
page.set_content('<div id="sample">Обычный текст для OCR</div>')
saved = capture_element(page, "#sample", timeout_ms=5000)
```

Функции возвращают `Path`. По умолчанию PNG сохраняются в `ocr_images/`
рядом с модулем, можно передать `output_dir`. Имена содержат UUID;
существующие файлы не перезаписываются. Пустой, повреждённый или отсутствующий
PNG, недоступный каталог и ошибка захвата дают `ImageCaptureError`.
Если селектор соответствует нескольким элементам, захват завершится ошибкой.

Проект использует синхронный Playwright: `open_profile()` открывает профиль
BitBrowser, затем `chromium.connect_over_cdp(ws)` подключается к нему.
`hh_searchapply.snapshot()` сохраняет скриншот всей страницы и текст в
`tests/artifacts/`. Новый модуль получает готовую страницу и снимает только
выбранный элемент; сам к профилям не подключается.

Обработка капчи остаётся в `hh_autoapply.py`: `CAPTCHA_RE` и `CAPTCHA_SEL`
задают признаки, `captcha_present()` проверяет URL и видимость через
`visible()` / `visible_text()`, а `wait_captcha()` ждёт ручного решения.
Новый модуль не вызывается из этого кода.

При первом обнаружении капчи `captcha_present()` сохраняет видимую картинку
или контейнер в `captcha_images/`. Если стабильного DOM-элемента нет,
сохраняется вся страница. В течение одного непрерывного показа создаётся один
файл; после исчезновения следующая капча снова будет сохранена. Каталог можно
переопределить переменной `CAPTCHA_IMAGE_DIR`. Сбор не запускает OCR и не
меняет ручное ожидание `wait_captcha()`.

Проверка без браузера:
`venv/bin/python -m unittest discover -s tests -p 'test_ocr_engine.py'`.

## Распознавание

На macOS установи движок и языки: `brew install tesseract tesseract-lang`.
Проверь `tesseract --version` (нужна версия 5) и `tesseract --list-langs`.
Python-зависимости устанавливаются командой в начале документа.
Используется OpenCV без GUI (`opencv-python-headless`).

```python
from ocr_engine import recognize, OCRError

try:
    result = recognize("example.png", lang="rus+eng", preprocess=True,
                       grayscale=True, clahe=False,
                       gaussian_blur=False, otsu=True,
                       psm=6, oem=3, timeout=30,
                       config="-c preserve_interword_spaces=1",
                       stats_csv="ocr_stats.csv")
    print(result.text)
    print(result.loading_seconds, result.preprocessing_seconds, result.recognition_seconds,
          result.total_seconds)
except OCRError as exc:
    print(exc)
```

Вход: путь к PNG, PNG-байты, Pillow Image или `uint8` NumPy-массив.
`preprocess=False` отключает всю предобработку OpenCV; её время будет нулевым.
При `preprocess=True` фильтры управляются независимо: `grayscale`, `clahe`,
`gaussian_blur`, `otsu`. Быстрый набор по умолчанию — grayscale + Otsu;
CLAHE и Gaussian Blur выключены. Если Otsu включён без grayscale, необходимое
преобразование в серый выполняется непосредственно перед порогом. CLAHE без
grayscale обрабатывает только канал яркости LAB. Gaussian Blur использует ядро
3×3. Не включай фильтр без измеримого улучшения результата.
`psm` задаёт разметку текста (6 — блок, 7 — строка), `oem` — режим движка.
Дополнительные параметры `config` передаются Tesseract, поэтому не дублируй
в них `--psm` и `--oem` без необходимости. Отсутствующий язык, движок,
неподходящая версия, повреждённый вход и таймаут дают `OCRError`.
Пустой распознанный текст возвращается как обычный результат.

Время возвращается в секундах: загрузка, предобработка, вызов OCR и общее время.
Проверка версии Tesseract выполняется один раз за процесс и кешируется. Если
передан путь и `preprocess=False`, проверенный PNG передаётся Tesseract напрямую,
без декодирования и повторного кодирования через Pillow.
`stats_csv` добавляет строку в CSV с настройками, временами и числом символов;
заголовок создаётся автоматически. По умолчанию статистика не записывается.
Вызов OCR включает сериализацию изображения и запуск процесса Tesseract:
pytesseract запускает Tesseract как отдельный процесс, поэтому загрузка языковой
модели внутри каждого OCR-вызова управляется самим Tesseract. На уровне Python
повторно не запускаются проверки версии и не выполняются лишние преобразования.
Интерфейс параметров соответствует [документации pytesseract](https://github.com/madmaze/pytesseract).

Все проверки OCR (включая распознавание обычного тестового текста):
`venv/bin/python -m unittest discover -s tests -p 'test_ocr*.py'`.
Тесты не подключаются к браузеру или hh.

## Polar unwrap

`polar_unwrap()` выпрямляет кольцевой сектор через `cv2.warpPolar` с
`WARP_POLAR_LINEAR`. Центр задаётся как `(x, y)`, радиус — в пикселях,
углы — в градусах. Диапазон `(300, 60)` проходит через ноль и означает 120°.
Центр дуги разрешено задавать за пределами обрезанного изображения.

```python
from ocr_engine import polar_unwrap

result = polar_unwrap("example.png", center=(336, 250), radius=220,
                      angle_range=(210, 330), output_dir="polar_images",
                      flip_horizontal=False)
print(result.original_path)
print(result.unwrapped_path)
# result.image — новый numpy-массив: угол горизонтально, радиус вертикально
```

Для каждого вызова сохраняется уникальная пара `*_original.png` и
`*_unwrapped.png`. Исходный объект и исходный файл не изменяются. Угловая ось,
которую `warpPolar` создаёт вертикальной, транспонируется в горизонтальную для
последующего OCR. Если строка идёт в обратном направлении, включи
`flip_horizontal=True`. Ошибочные параметры дают `ImageTransformError` и не оставляют
частичных файлов.

Для выпрямления с последующим сравнением Tesseract используй существующий
OCR-движок через `recognize_polar()`:

```python
from ocr_engine import recognize_polar

comparison = recognize_polar(
    "example.png", center=(336, 250), radius=220,
    angle_range=(210, 330), lang="rus+eng",
    flip_horizontal=True,
    grayscale=True, clahe=False, gaussian_blur=False, otsu=True,
)
print("PSM 7:", comparison.results[7].text)
print("PSM 13:", comparison.results[13].text)
print("Общая предобработка:", comparison.preprocessing_seconds)
```

Развёртка и фильтры вычисляются один раз. Подготовленный массив дважды
передаётся в `recognize(preprocess=False)`, с PSM 7 и PSM 13. Поэтому два
OCR-запуска не повторяют OpenCV-обработку.

### Зафиксированный пресет test.png

Для тестового изображения с эталоном `толокняная млея` параметры сохранены
в `CURVED_TEXT_PRESET`:

```python
from ocr_engine import CURVED_TEXT_PRESET, recognize_polar

result = recognize_polar(
    "captcha_images/test.png", lang="rus",
    output_dir="polar_images", **CURVED_TEXT_PRESET,
)
print(result.results[7].text)
```

Пресет: центр `(330, -20)`, радиус `245`, углы `(12, 170)`, горизонтальный
разворот, Grayscale + CLAHE + Otsu, без Gaussian Blur, радиальный диапазон
`(155, None)` и белое поле `25 px`. Основной результат берётся из PSM 7;
PSM 13 сохраняется для сравнения.
