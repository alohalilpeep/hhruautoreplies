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
# Версия резюме. Штампуется на каждый отклик, чтобы потом сравнивать
# конверсию разных редакций: поменял резюме — подними версию в .env.
RESUME_VERSION = os.getenv("RESUME_VERSION", "1.0").strip() or "1.0"
DRY_RUN = _flag("DRY_RUN", "True")   # True = только ходит и логирует, не откликается

# Что делать с сопроводительным письмом:
#   never    — не прикладывать никогда. Вакансии, где письмо обязательно,
#              не трогаем: статус needs_letter, ждут смены режима.
#   required — прикладывать только там, где работодатель требует. На остальные
#              откликаемся одним резюме.
#   always   — прикладывать везде, в том числе дописывать после мгновенного
#              отклика через «Приложить сопроводительное письмо».
# Отвечать ли на анкетные вопросы работодателя по шаблонам из answers.txt.
# Только если распознаны ВСЕ вопросы вакансии, иначе она откладывается.
AUTO_ANSWER = _flag("AUTO_ANSWER", "False")

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


CYR_RE = re.compile(r"[а-яё]", re.I)


def _gen_noun(word):
    """Существительное мужского рода в родительный падеж."""
    low = word.lower()
    if low.endswith(("ь", "й")):
        return word[:-1] + "я"        # руководитель -> руководителя
    if low.endswith("а"):
        return word[:-1] + "ы"        # редко, но пусть будет
    if low.endswith(("о", "е", "ы", "и")):
        return word                   # уже не именительный, не трогаем
    return word + "а"                 # инженер -> инженера


def _gen_adj(word):
    """Прилагательное мужского рода в родительный падеж."""
    low = word.lower()
    if low.endswith("ий"):
        return word[:-2] + "его"      # ведущий -> ведущего
    if low.endswith(("ый", "ой")):
        return word[:-2] + "ого"      # системный -> системного, сетевой -> сетевого
    return word


def _is_adj(word):
    return CYR_RE.search(word) and word.lower().endswith(("ый", "ий", "ой"))


# после предлога идёт зависимая часть, она уже в нужном падеже
PREPOSITIONS = {"в", "во", "на", "по", "для", "с", "со", "из", "от", "при",
                "до", "за", "к", "о", "об", "и"}


def _gen_token(word, as_adj):
    """Склонить слово, в том числе составное через дефис.
    «инженер-программист» -> обе части, «DevOps-инженер» -> только русскую."""
    if not CYR_RE.search(word):
        return word                   # латиница и цифры как есть
    if "-" in word:
        parts = [_gen_noun(p) if CYR_RE.search(p) else p
                 for p in word.split("-")]
        return "-".join(parts)
    return _gen_adj(word) if as_adj else _gen_noun(word)


def to_genitive(title):
    """Название должности в родительный падеж: «заинтересовала вакансия X».

    Склоняем только ведущую группу «прилагательные + первое существительное».
    Всё, что идёт дальше, у hh уже стоит в нужном падеже и трогать его нельзя:
    «руководитель направления DevOps», «инженер по инфраструктуре».
    Чисто латинские названия возвращаются без изменений.
    """
    # «пентестер - исследователь»: дефис с пробелами это одно слово
    title = re.sub(r"\s+-\s+", "-", title)
    tokens = title.split()
    out, noun_done = [], False
    for tok in tokens:
        if tok.lower() in PREPOSITIONS:
            out.append(tok)           # сам предлог не склоняем,
            noun_done = True          # и всё после него тоже
            continue
        if noun_done or not CYR_RE.search(tok):
            out.append(tok)           # латиница-модификатор или хвост
            continue
        if _is_adj(tok):
            out.append(_gen_token(tok, as_adj=True))
        else:
            out.append(_gen_token(tok, as_adj=False))
            noun_done = True
    result = " ".join(out)
    if result and CYR_RE.match(result[0]):
        result = result[0].lower() + result[1:]   # середина предложения
    return result


MIN_TITLE_LEN = 5
# короткие, но настоящие должности: длиной их не отфильтруешь
SHORT_ROLE_OK = {"sre", "qa", "noc", "devops", "mlops", "devsecops", "sysops"}
# голое название технологии это не должность: «позиция «Python»» читается плохо
BARE_TECH = {"python", "java", "golang", "go", "php", "linux", "kubernetes",
             "javascript", "typescript", "node.js", "react", "c++", "c#", ".net",
             "bash", "docker", "terraform", "ansible"}
# когда подставлять нечего, убираем вместе с предшествующим «вакансия»,
# иначе выйдет «заинтересовала вакансия ваша вакансия»
NO_TITLE_RE = re.compile(r"(?:вакансия|позиция)\s+[«\"']?\{title\}[»\"']?", re.I)
NO_TITLE_PHRASE = "ваша вакансия"


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
    if usable_title(title):
        title = to_genitive(title)   # «вакансия системного администратора»
    elif NO_TITLE_RE.search(text):
        text = NO_TITLE_RE.sub(NO_TITLE_PHRASE, text)
        title = ""
    else:
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
    "task_question": '[data-qa="task-question"]',
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

# ============ ответы на вопросы работодателей ============
ANSWERS_FILE = Path(__file__).with_name("answers.txt")

# Темы анкетных вопросов. Правила намеренно узкие: лучше не распознать вопрос
# и отложить вакансию, чем ответить на него не то. Например «Есть опыт работы
# с 3proxy?» — технический вопрос, а не «сколько лет опыта», и попадать
# в тему «опыт» он не должен.
QUESTION_TOPICS = {
    "зарплата": re.compile(
        # основа «уров», а не «уровн»: в слове «уровень» после «уров» идёт «е»
        r"зарплат|заработн\w*\s+плат|оклад|вилк|з/п|\bзп\b"
        r"|уров\w*\s+дохода|доход\w*\s+(по\s+)?окладн|ожидани\w+\s+по\s+доход", re.I),
    "город": re.compile(
        r"в\s+каком\s+городе|город\w*\s+проживани|ваш\s+город"
        r"|где\s+вы\s+(сейчас\s+)?(живёте|живете|проживаете|находитесь)", re.I),
    "опыт": re.compile(
        r"сколько\s+лет|как\s+давно\s+(ты|вы)\b|стаж\s+работы"
        r"|сколько\s+\w*\s*лет\s+опыта", re.I),
    "формат работы": re.compile(
        r"формат\w*\s+работы|удал[её]нн\w*\s+формат|офис\s*/\s*гибрид", re.I),
    "телеграм": re.compile(
        r"логин\s+в\s+телеграм|ник\s+в\s+телеграм|телеграм\w*\s+для\s+связи"
        r"|укажите\s+ваш\s+телеграм", re.I),
}


# В одно поле часто кладут два вопроса сразу: «В каком городе проживаете?
# Рассматриваете удалённый формат или гибрид?». Шаблон закроет только половину,
# поэтому такие пропускаем целиком.
MAX_QUESTION_LEN = 140

# Признаки того, что рядом с нашей темой спрашивают ещё что-то, чего мы
# не умеем: «укажите зарплатные ожидания И желаемый формат сотрудничества».
SECOND_ASK_RE = re.compile(
    r"формат\w*\s+(сотрудничеств|занятост)|трудов\w+\s+договор|самозанят"
    r"|\bИП\b|гражданств|готов\w*\s+приступить|когда\s+готов|испытательн"
    r"|сколько\s+вам\s+лет", re.I)


def is_compound(text):
    """Вопрос спрашивает больше одной вещи — шаблонным ответом не закрыть.

    Союз «и» — самый надёжный признак: «укажите локацию (город) И формат
    работы», «зарплатные ожидания И желаемый формат сотрудничества».
    Одиночные анкетные вопросы его почти не содержат.
    """
    flat = re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()
    return (flat.count("?") > 1
            or len(flat) > MAX_QUESTION_LEN
            or bool(re.search(r"\sи\s", flat, re.I))
            or bool(SECOND_ASK_RE.search(flat)))


def normalize_question(text):
    """Ключ для банка ответов: без регистра, пунктуации и лишних пробелов,
    чтобы один и тот же вопрос у разных работодателей нашёлся."""
    t = (text or "").lower().replace("\xa0", " ")
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


BLACKLIST_FILE = Path(__file__).with_name("blacklist.txt")


def load_blacklist():
    """Компании, которым не откликаемся. Совпадение по части названия."""
    try:
        raw = BLACKLIST_FILE.read_text(encoding="utf-8")
    except OSError:
        return []
    return [l.strip().lower() for l in raw.splitlines()
            if l.strip() and not l.startswith("#")]


BLACKLIST = load_blacklist()


def is_blacklisted(company):
    low = (company or "").lower()
    return bool(low) and any(b in low for b in BLACKLIST)


def load_skipped_questions(db):
    """Вопросы, помеченные skip: вакансии с ними не трогаем."""
    if db is None:
        return set()
    try:
        return {q for (q,) in db.execute(
            "SELECT qnorm FROM answer_bank WHERE status='skip'")}
    except sqlite3.OperationalError:
        return set()


def load_answer_bank(db):
    """Одобренные вручную ответы. Ключ — нормализованный вопрос."""
    if db is None:
        return {}
    try:
        rows = db.execute(
            "SELECT qnorm, answer FROM answer_bank WHERE status='approved'")
    except sqlite3.OperationalError:
        return {}
    return {q: a for q, a in rows if a and a.strip()}


def classify_question(text):
    """Тема вопроса или None, если шаблонного ответа для него нет."""
    flat = re.sub(r"\s+", " ", (text or "").replace("\xa0", " "))
    if is_compound(flat):
        return None
    for topic, pat in QUESTION_TOPICS.items():
        if pat.search(flat):
            return topic
    return None


def load_answers():
    """answers.txt: строки вида «тема: ответ». Пустой или отсутствующий файл
    означает, что автоответы просто не сработают."""
    answers = {}
    try:
        raw = ANSWERS_FILE.read_text(encoding="utf-8")
    except OSError:
        return answers
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        topic, _, value = line.partition(":")
        topic, value = topic.strip().lower(), value.strip()
        if topic and value:
            answers[topic] = value
    return answers


ANSWERS = load_answers()

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
    # версия резюме на момент отклика; в старых базах колонки нет
    cols = {r[1] for r in db.execute("PRAGMA table_info(responses)")}
    if "resume_version" not in cols:
        db.execute("ALTER TABLE responses ADD COLUMN resume_version TEXT")
    if "letter_sent" not in cols:
        db.execute("ALTER TABLE responses ADD COLUMN letter_sent INTEGER")
    # пул вопросов работодателей: копится сам, пока скрипт откладывает questions
    db.execute("""CREATE TABLE IF NOT EXISTS questions (
        vacancy_id TEXT, url TEXT, company TEXT, idx INTEGER,
        question TEXT, kind TEXT, options TEXT, ts TEXT,
        PRIMARY KEY (vacancy_id, idx))""")
    # Вопросы робота-рекрутера из чатов hh. Отдельно от questions: там анкета
    # вакансии со свободным вводом, здесь диалог с готовыми вариантами ответа.
    db.execute("""CREATE TABLE IF NOT EXISTS chat_questions (
        chat_id TEXT, msg_id TEXT, company TEXT, vacancy TEXT,
        question TEXT, options TEXT, ts TEXT,
        PRIMARY KEY (chat_id, msg_id))""")
    # банк ответов для чатов, тоже отдельный
    db.execute("""CREATE TABLE IF NOT EXISTS chat_answers (
        qnorm TEXT PRIMARY KEY, question TEXT, options TEXT,
        answer TEXT, status TEXT, ts TEXT)""")
    # банк готовых ответов: заполняется вручную через hh_answers.py.
    # Ключ — нормализованный текст вопроса, поэтому повторы переиспользуются.
    db.execute("""CREATE TABLE IF NOT EXISTS answer_bank (
        qnorm TEXT PRIMARY KEY, question TEXT, answer TEXT,
        status TEXT, ts TEXT)""")
    return db


def save_questions(db, vid, url, company, questions):
    """Сложить вопросы работодателя в пул. Отклик при этом не отправляется."""
    ts = dt.datetime.now().isoformat(timespec="seconds")
    for i, q in enumerate(questions):
        db.execute(
            "INSERT OR REPLACE INTO questions VALUES (?,?,?,?,?,?,?,?)",
            (vid, url, company, i, q["question"], q["kind"],
             " | ".join(q["options"]), ts))
    db.commit()


def save(db, vid, url, title, company, status, letter_sent=False):
    db.execute(
        "INSERT OR REPLACE INTO responses "
        "(id, url, title, company, status, ts, resume_version, letter_sent) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (vid, url, title, company, status,
         dt.datetime.now().isoformat(timespec="seconds"), RESUME_VERSION,
         1 if letter_sent else 0))
    db.commit()


def applied_today(db):
    # answered это тоже отправленный отклик, просто с заполненной анкетой
    today = dt.date.today().isoformat()
    return db.execute(
        "SELECT COUNT(*) FROM responses "
        "WHERE status IN ('applied', 'answered') AND ts >= ?",
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


def scrape_questions(page):
    """Снять вопросы работодателя со страницы отклика. Ничего не отправляет.

    Текст лежит в task-question, а поле ответа рядом, внутри общего task-body,
    поэтому за единицу берём task-body и ищем вопрос внутри него.
    """
    try:
        return page.evaluate("""() => {
            const bodies = [...document.querySelectorAll('[data-qa="task-body"]')];
            return bodies.map(b => {
                const q = b.querySelector('[data-qa="task-question"]') || b;
                const inputs = [...b.querySelectorAll('input, textarea, select')];
                const kinds = [...new Set(inputs.map(i =>
                    i.tagName === 'TEXTAREA' ? 'textarea'
                    : i.tagName === 'SELECT' ? 'select'
                    : (i.type || 'text')))];
                const opts = [...b.querySelectorAll('label')]
                    .map(l => (l.innerText || '').trim())
                    .filter(Boolean);
                return {
                    question: (q.innerText || '').trim(),
                    kind: kinds.join(',') || 'unknown',
                    options: opts.slice(0, 12)
                };
            }).filter(q => q.question);
        }""")
    except Exception:
        return []


def answer_questions(page, questions, bank=None):
    """Заполнить анкету работодателя и отправить отклик.

    Источники ответа, в порядке приоритета:
      1. банк одобренных ответов из базы (точный вопрос),
      2. шаблон по теме из answers.txt (зарплата, город и т.п.).

    Возвращает None, если отвечать нельзя — тогда вакансия откладывается как
    раньше. Отказываемся, если: на вопрос нет ни одобренного ответа, ни темы;
    поле не текстовое (радиокнопку за человека выбирать нельзя); или число полей
    не сошлось с числом вопросов.
    """
    if not AUTO_ANSWER or not questions:
        return None
    bank = bank or {}

    plan = []
    for q in questions:
        if "textarea" not in q["kind"] and "text" not in q["kind"]:
            return None
        approved = bank.get(normalize_question(q["question"]))
        if approved:
            plan.append(approved)
            continue
        topic = classify_question(q["question"])
        if not topic or topic not in ANSWERS:
            return None
        plan.append(ANSWERS[topic])

    bodies = page.locator(SEL["task"])
    if bodies.count() != len(plan):
        return None               # разметка не сошлась, не рискуем

    for i, text in enumerate(plan):
        field = bodies.nth(i).locator("textarea, input[type=text]").first
        if field.count() == 0:
            return None
        field.fill(text)
        pause(0.5, 1.5)

    submit = page.locator(SEL["popup_submit"])
    if submit.count() == 0:
        return None
    submit.first.click()
    pause(3, 5)

    if page.get_by_text(LIMIT_RE).count():
        raise LimitReached()
    if page.locator(SEL["already"]).count() or page.get_by_text(DONE_RE).count():
        return "answered"
    return "unknown"


def close_popup(page):
    """Закрыть попап отклика, ничего не отправляя."""
    btn = page.locator(SEL["popup_close"])
    if btn.count():
        btn.first.click()
    else:
        page.keyboard.press("Escape")
    pause(1, 2)


# Письмо, приложенное к последнему отклику. Скрипт однопоточный, поэтому
# флаг модуля надёжнее, чем тащить четвёртый элемент через все ветки возврата.
LAST_LETTER_SENT = False


def apply(page, url, db=None):
    global LAST_LETTER_SENT
    LAST_LETTER_SENT = False
    page.goto(url, wait_until="domcontentloaded")
    pause(2, 4)
    title = text_or(page, SEL["title"])
    company = text_or(page, SEL["company"])

    # компания в стоп-листе: не откликаемся, ничего не кликаем
    if is_blacklisted(company):
        return "blacklisted", title, company

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

    # вакансия с вопросами или тестом, оставляем на ручной разбор.
    # Заодно складываем вопросы в пул: их видно только отсюда.
    if "vacancy_response" in page.url or page.locator(SEL["task"]).count():
        qs = scrape_questions(page)
        if db is not None and qs:
            m = re.search(r"/vacancy/(\d+)", url)
            save_questions(db, m.group(1) if m else url, url, company, qs)
            print(f"    собрано вопросов: {len(qs)}")
        # среди вопросов есть отброшенный вручную — вакансия не наша
        dropped = load_skipped_questions(db)
        if dropped and any(normalize_question(q["question"]) in dropped
                           for q in qs):
            return "skipped_question", title, company

        answered = answer_questions(page, qs, load_answer_bank(db))
        if answered:
            print(f"    анкета заполнена, вопросов: {len(qs)}")
            return answered, title, company
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
        LAST_LETTER_SENT = letter_sent
        return "applied", title, company
    LAST_LETTER_SENT = letter_sent
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
                    status, title, company = apply(page, url, db)
                except LimitReached:
                    print("hh пишет, что лимит откликов исчерпан")
                    break
                except Exception as e:
                    status, title, company = "error", "", ""
                    print(f"Ошибка на {url}: {e}")

                save(db, vid, url, title, company, status, LAST_LETTER_SENT)
                mark = " +письмо" if LAST_LETTER_SENT else ""
                print(f"[{status}{mark}] {title} | {company} | {url}")
                pause(20, 60) if status == "applied" else pause(3, 8)

            today = dt.date.today().isoformat()
            with_letter = db.execute(
                "SELECT COUNT(*) FROM responses WHERE letter_sent=1 "
                "AND status IN ('applied','answered') AND ts >= ?",
                (today,)).fetchone()[0]
            total_today = applied_today(db)
            print(f"\nЗа сегодня откликов: {total_today}, "
                  f"из них с сопроводительным: {with_letter}, "
                  f"без письма: {total_today - with_letter}")
    finally:
        close_profile()


if __name__ == "__main__":
    main()
