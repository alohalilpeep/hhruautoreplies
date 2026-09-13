"""
Автоотклики на hh.ru через профиль BitBrowser.
pip install -r requirements.txt
(playwright install не нужен, браузер берётся из BitBrowser)

Настройки — в .env рядом со скриптом, шаблон в .env.example.
"""
import os
import random
import re
import sqlite3
import time
import datetime as dt
from pathlib import Path

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

load_dotenv(Path(__file__).with_name(".env"))


def _flag(name, default):
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


# ================= НАСТРОЙКИ =================
# значения берутся из .env рядом со скриптом, см. .env.example
BIT_API = os.getenv("BIT_API", "http://127.0.0.1:54345")
BIT_API_KEY = os.getenv("BIT_API_KEY", "")   # если в настройках Local API включён ключ
PROFILE_ID = os.getenv("PROFILE_ID", "")

# настрой поиск на hh руками со всеми фильтрами и положи URL в .env
SEARCH_URL = os.getenv("SEARCH_URL", "")
MAX_PAGES = int(os.getenv("MAX_PAGES", "5"))          # сколько страниц выдачи обходить
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", "50"))     # свой лимит откликов в сутки
RESUME_TITLE = os.getenv("RESUME_TITLE", "")          # часть названия резюме, если их несколько
DRY_RUN = _flag("DRY_RUN", "True")   # True = только ходит и логирует, не откликается

# Что делать с сопроводительным письмом:
#   never    — не прикладывать никогда. Вакансии, где письмо обязательно,
#              не трогаем: статус needs_letter, ждут смены режима.
#   required — прикладывать только там, где работодатель требует. На остальные
#              откликаемся одним резюме.
#   always   — прикладывать везде, в том числе дописывать после мгновенного
#              отклика через «Приложить сопроводительное письмо».
LETTER_MODE = os.getenv("LETTER_MODE", "required").strip().lower()
if LETTER_MODE not in ("never", "required", "always"):
    raise SystemExit(
        f"LETTER_MODE={LETTER_MODE!r} — допустимо never, required или always")

# Текст сопроводительного письма лежит в letter.txt рядом со скриптом и
# не коммитится: там личные контакты. Шаблон — letter.example.txt.
# Поддерживаются подстановки {title} и {company}, но они необязательны.
LETTER_FILE = Path(__file__).with_name("letter.txt")

DEFAULT_LETTER = """Здравствуйте!

Меня заинтересовала вакансия «{title}» в {company}. Мой опыт хорошо подходит под ваши задачи, подробности в резюме.

Буду рад обсудить.
"""


def _load_letter():
    try:
        text = LETTER_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return DEFAULT_LETTER
    return text or DEFAULT_LETTER


LETTER = _load_letter()


# грейд в начале названия: «Middle DevOps-инженер» -> «DevOps-инженер»
GRADE_RE = re.compile(
    r"^(младш\w*|старш\w*|ведущ\w*|главн\w*|стаж[ёе]р\w*"
    r"|junior|middle|senior|lead|jun|mid|sr|jr)\b[\s.\-–—]*", re.I)


def _strip_grade(text):
    t = re.sub(r"\s+", " ", text).strip(" .,-–—+")
    prev = None
    while prev != t:                             # «Ведущий старший инженер»
        prev = t
        t = GRADE_RE.sub("", t).strip(" .,-–—+")
    if t and t[0].islower():
        t = t[0].upper() + t[1:]
    return t


def clean_title(title):
    """Название вакансии для письма. hh кладёт в заголовок грейд, стек в скобках,
    город, сроки проекта, альтернативы через слэш и слоган компании после «|».
    В письме нужно только само название должности."""
    base = title.split("|")[0]                       # «... | Компания | слоган»
    base = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", base)  # «(Linux)», «[МТС]»
    # «Senior/Middle Python»: до слэша один грейд, после очистки пусто —
    # тогда берём строку целиком, без разбиения по слэшу
    for candidate in (base.split("/")[0], base.replace("/", " ")):
        cleaned = _strip_grade(candidate)
        if cleaned:
            return cleaned
    return re.sub(r"\s+", " ", title).strip()


MIN_TITLE_LEN = 5
# короткие, но настоящие должности: длиной их не отфильтруешь
SHORT_ROLE_OK = {"sre", "qa", "noc", "devops", "mlops", "devsecops", "sysops"}
# голое название технологии это не должность: «позиция «Python»» читается плохо
BARE_TECH = {"python", "java", "golang", "go", "php", "linux", "kubernetes",
             "javascript", "typescript", "node.js", "react", "c++", "c#", ".net",
             "bash", "docker", "terraform", "ansible"}
# чем заменить «{title}», когда подставлять нечего
NO_TITLE_PHRASE = "в вашей компании"


def usable_title(title):
    """Годится ли очищенное название, чтобы вставить его в кавычках в письмо."""
    t = (title or "").strip()
    if not t:
        return False
    low = t.lower()
    if low in BARE_TECH:
        return False
    if low in SHORT_ROLE_OK:
        return True
    return len(t) >= MIN_TITLE_LEN


def render_letter(title, company):
    """Подставить название и компанию. Письмо без подстановок вернётся как есть,
    кривые фигурные скобки в тексте не должны ронять отклик."""
    text = LETTER
    if not usable_title(title):
        # убираем вместе с кавычками, иначе выйдет «позиция «ваша вакансия»»
        text = re.sub(r"[«\"']?\{title\}[»\"']?", NO_TITLE_PHRASE, text)
        title = ""
    try:
        return text.format(title=title,
                           company=company or "вашей компании").strip()
    except (KeyError, IndexError, ValueError):
        return text.strip()

# селекторы hh (data-qa). Если что-то перестало находиться, открой DevTools и поправь здесь
SEL = {
    "serp_link": 'a[data-qa="serp-item__title"]',
    "title": '[data-qa="vacancy-title"]',
    "company": '[data-qa="vacancy-company-name"]',
    "apply_btn": '[data-qa="vacancy-response-link-top"]',
    "already": '[data-qa="vacancy-response-link-view-topic"]',
    "relocation_confirm": '[data-qa="relocation-warning-confirm"]',
    "task": '[data-qa="task-body"]',
    # попап отклика теперь bottom-sheet, старый vacancy-response-popup не находится
    "popup": '[data-qa="bottom-sheet-content"]',
    "popup_close": '[data-qa="response-popup-close"]',
    "hidden_resume": '[data-qa="hidden-resume-warning"]',
    "popup_submit": '[data-qa="vacancy-response-submit-popup"]',
    "letter_toggle": '[data-qa="vacancy-response-letter-toggle"]',
    "letter_input": '[data-qa="vacancy-response-popup-form-letter-input"]',
    # письмо после мгновенного отклика: hh показывает кнопку прямо на вакансии,
    # по ней открывается та же форма письма, что и в попапе
    "letter_after_toggle": '[data-qa="responded-success-attach-cover-letter"]',
    "letter_after_input": '[data-qa="vacancy-response-popup-form-letter-input"]',
    "letter_after_submit": '[data-qa="vacancy-response-letter-submit"]',
    "login_link": '[data-qa="login"]',
}

LIMIT_RE = re.compile(r"не более \d+ откликов|лимит откликов", re.I)
DONE_RE = re.compile(r"Вы откликнулись|Резюме доставлено|Отклик отправлен", re.I)
# работодатель требует сопроводительное письмо. Формулировка hh в попапе отклика:
# «Сопроводительное письмо ... Обязательное поле для этой вакансии»
LETTER_REQUIRED_RE = re.compile(
    r"обязательное\s+поле\s+для\s+этой\s+вакансии"
    r"|сопроводительн\w*\s+письмо\s+обязательн"
    r"|обязательн\w*\s+сопроводительн\w*\s+письмо", re.I)


class LimitReached(Exception):
    pass


def pause(a, b):
    time.sleep(random.uniform(a, b))


# ================= BitBrowser =================
def _headers():
    return {"x-api-key": BIT_API_KEY} if BIT_API_KEY else {}


def open_profile():
    r = requests.post(f"{BIT_API}/browser/open", json={"id": PROFILE_ID},
                      headers=_headers(), timeout=60).json()
    if not r.get("success"):
        raise RuntimeError(f"BitBrowser не открыл профиль: {r}")
    return r["data"]["ws"]


def close_profile():
    try:
        requests.post(f"{BIT_API}/browser/close", json={"id": PROFILE_ID},
                      headers=_headers(), timeout=30)
    except Exception:
        pass


# ================= База =================
def init_db():
    db = sqlite3.connect("hh_responses.db")
    db.execute("""CREATE TABLE IF NOT EXISTS responses (
        id TEXT PRIMARY KEY, url TEXT, title TEXT, company TEXT, status TEXT, ts TEXT)""")
    return db


def save(db, vid, url, title, company, status):
    db.execute("INSERT OR REPLACE INTO responses VALUES (?,?,?,?,?,?)",
               (vid, url, title, company, status, dt.datetime.now().isoformat(timespec="seconds")))
    db.commit()


def applied_today(db):
    today = dt.date.today().isoformat()
    return db.execute("SELECT COUNT(*) FROM responses WHERE status='applied' AND ts >= ?",
                      (today,)).fetchone()[0]


# ================= hh =================
def text_or(page, sel, default=""):
    try:
        return page.locator(sel).first.inner_text(timeout=3000).strip()
    except PWTimeout:
        return default


def collect(page):
    found = {}
    sep = "&" if "?" in SEARCH_URL else "?"
    for n in range(MAX_PAGES):
        page.goto(f"{SEARCH_URL}{sep}page={n}", wait_until="domcontentloaded")
        pause(2, 4)
        links = page.locator(SEL["serp_link"])
        if links.count() == 0:
            break
        for href in links.evaluate_all("els => els.map(e => e.href)"):
            m = re.search(r"/vacancy/(\d+)", href)
            if m:
                found[m.group(1)] = f"https://hh.ru/vacancy/{m.group(1)}"
    return found


def visible(page, sel):
    """Элемент есть И показан. hh держит предупреждения в DOM всегда, схлопывая
    их в max-height:0, поэтому одного .count() недостаточно."""
    loc = page.locator(sel)
    return bool(loc.count()) and loc.first.is_visible()


def visible_text(page, pattern):
    """То же для поиска по тексту."""
    loc = page.get_by_text(pattern)
    return bool(loc.count()) and loc.first.is_visible()


def close_popup(page):
    """Закрыть попап отклика, ничего не отправляя."""
    btn = page.locator(SEL["popup_close"])
    if btn.count():
        btn.first.click()
    else:
        page.keyboard.press("Escape")
    pause(1, 2)


def apply(page, url):
    page.goto(url, wait_until="domcontentloaded")
    pause(2, 4)
    title = text_or(page, SEL["title"])
    company = text_or(page, SEL["company"])

    if page.locator(SEL["already"]).count():
        return "already", title, company
    btn = page.locator(SEL["apply_btn"]).first
    if btn.count() == 0:
        return "no_button", title, company
    if DRY_RUN:
        return "dry_run", title, company

    pages_before = len(page.context.pages)
    btn.click()
    pause(2, 4)

    # новая вкладка = отклик на сайте работодателя, пропускаем
    if len(page.context.pages) > pages_before:
        for p in page.context.pages[pages_before:]:
            p.close()
        return "external", title, company

    if page.get_by_text(LIMIT_RE).count():
        raise LimitReached()

    # предупреждение "вакансия в другом городе"
    reloc = page.locator(SEL["relocation_confirm"])
    if reloc.count():
        reloc.first.click()
        pause(1, 2)

    # вакансия с вопросами или тестом, оставляем на ручной разбор
    if "vacancy_response" in page.url or page.locator(SEL["task"]).count():
        return "questions", title, company

    # в базу пишем сырой заголовок, в письмо — очищенный
    letter = render_letter(clean_title(title), company)
    letter_sent = False

    submit = page.locator(SEL["popup_submit"])
    if submit.count():
        if RESUME_TITLE:
            scope = page.locator(SEL["popup"]) if page.locator(SEL["popup"]).count() else page
            scope.get_by_text(RESUME_TITLE, exact=False).first.click()
            pause(0.5, 1.5)

        # резюме скрыто от работодателей, отклик не примут: не трогаем.
        # Блок всегда есть в DOM и схлопнут, поэтому проверяем видимость.
        if visible(page, SEL["hidden_resume"]):
            close_popup(page)
            return "resume_hidden", title, company

        letter_required = visible_text(page, LETTER_REQUIRED_RE)

        # письмо обязательно, а режим его запрещает: закрываем попап
        # и откладываем вакансию, ничего не отправив
        if letter_required and LETTER_MODE == "never":
            close_popup(page)
            return "needs_letter", title, company

        # пишем, только если работодатель требует или режим always
        if letter_required or LETTER_MODE == "always":
            toggle = page.locator(SEL["letter_toggle"])
            if toggle.count():
                toggle.first.click()
                pause(0.5, 1.5)
            area = page.locator(SEL["letter_input"])
            if area.count():
                area.first.fill(letter)
                letter_sent = True
                pause(1, 2)
        submit.first.click()
        pause(2, 4)
        if page.get_by_text(LIMIT_RE).count():
            raise LimitReached()

    # Отклик ушёл мгновенно, без попапа. Здесь письмо работодателем не требуется
    # (иначе hh показал бы попап), поэтому дописываем только в режиме always.
    # hh рисует кнопку «Приложить сопроводительное письмо», по ней открывается
    # форма. Без этого клика поля письма на странице просто нет.
    if LETTER_MODE == "always" and not letter_sent:
        toggle_after = visible(page, SEL["letter_after_toggle"])
        if toggle_after:
            page.locator(SEL["letter_after_toggle"]).first.click()
            pause(1, 2)
            after = page.locator(SEL["letter_after_input"])
            if after.count() and after.first.is_visible():
                after.first.fill(letter)
                pause(1, 2)
                page.locator(SEL["letter_after_submit"]).first.click()
                pause(2, 3)
                letter_sent = True

    if page.locator(SEL["already"]).count() or page.get_by_text(DONE_RE).count():
        return "applied", title, company
    return "unknown", title, company


# ================= main =================
def main():
    missing = [k for k in ("PROFILE_ID", "SEARCH_URL") if not globals()[k]]
    if missing:
        raise SystemExit(
            f"Не заданы {', '.join(missing)}. Скопируй .env.example в .env и заполни.")

    db = init_db()
    ws = open_profile()
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(ws)
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            page.goto("https://hh.ru", wait_until="domcontentloaded")
            pause(2, 3)
            if page.locator(SEL["login_link"]).count():
                print("Профиль не залогинен на hh.ru, зайди руками и перезапусти")
                return

            vacancies = collect(page)
            # needs_letter ждёт SEND_LETTER=True, resume_hidden — смены видимости
            # резюме. Обработанными их не считаем, вернутся на следующем запуске.
            done = {r[0] for r in db.execute(
                "SELECT id FROM responses WHERE status NOT IN "
                "('error', 'dry_run', 'needs_letter', 'resume_hidden')")}
            todo = [(vid, url) for vid, url in vacancies.items() if vid not in done]
            print(f"Найдено {len(vacancies)}, новых {len(todo)}")

            for vid, url in todo:
                if applied_today(db) >= DAILY_LIMIT:
                    print("Свой дневной лимит достигнут")
                    break
                try:
                    status, title, company = apply(page, url)
                except LimitReached:
                    print("hh пишет, что лимит откликов исчерпан")
                    break
                except Exception as e:
                    status, title, company = "error", "", ""
                    print(f"Ошибка на {url}: {e}")

                save(db, vid, url, title, company, status)
                print(f"[{status}] {title} | {company} | {url}")
                pause(20, 60) if status == "applied" else pause(3, 8)
    finally:
        close_profile()


if __name__ == "__main__":
    main()
