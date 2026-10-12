"""Вопросы ботов hh из чатов — в общий бланк ответов, ответы — обратно в чат.

Ботов два, и они разные:

  ИИ-помощник hh    «Я ИИ-помощник hh… задам несколько уточняющих вопросов».
                    Спрашивает свободным текстом, отвечаем текстом.
                    → часть 3 бланка
  Робот-рекрутер    «Робот-рекрутер присоединился к чату», анкета работодателя.
                    Ответ часто выбирается кнопкой.
                    → часть 4 бланка, с ВАРИАНТЫ / ВЫБОР, если есть кнопки

    python scripts/ai_chats.py collect [N]
        пройти N свежих чатов (по умолчанию 30) на всех трёх аккаунтах и
        запомнить, где бот ждёт ответа и на какой вопрос
    python scripts/ai_chats.py export
        дописать эти вопросы частями 3 и 4 в конец свежего бланка (после
        questions.sh export). Вопрос, на который ответ уже известен, в бланк
        не попадает — на него take ответит сразу
    python scripts/ai_chats.py take [--dry]
        забрать ответы из частей 3 и 4 и отправить их в чаты: текст — в поле
        ввода, выбор — нажатием кнопки. --dry только показывает
    python scripts/ai_chats.py peek <hh|hh2|hh3> <компания или номер чата> ...
        прочитать чаты целиком, с кнопками

Как узнать бота. У сообщений ИИ-помощника нет имени автора, и hh помечает их
так же, как собственные, поэтому узнаём по тексту. Кнопки внизу чата бывают
двух видов: ответы робота («Да / Нет», варианты анкеты) и стандартные
подсказки hh («Какая схема оплаты?», «Могу работать в гибком графике») —
это заготовки твоих реплик работодателю, ответом они не являются и
отфильтровываются по списку GENERIC_BUTTONS.

Бот спрашивает по одному: на ответ присылает следующий вопрос. Цикл поэтому
повторяется: collect → export → ответил → take → collect…

Открытие чата снимает у него отметку «непрочитано». Работодатель этого не
видит, счётчик непрочитанных только твой.
"""
import datetime as dt
import os
import re
import subprocess
import sys
from pathlib import Path

BASE = "/Users/mr_macbook/Documents"
ACCS = {"hh": "hh_autoreply", "hh2": "hh_autoreply_2", "hh3": "hh_autoreply_3"}

# Сообщения чата вместе с автором. hh подписывает автора только у первого
# сообщения серии, поэтому последнего известного тянем вперёд.
MSGS_JS = """() => {
    const out = [];
    let author = '';
    for (const el of document.querySelectorAll('[data-qa^="chatik-chat-message-"]')) {
        const qa = el.getAttribute('data-qa') || '';
        if (qa.endsWith('-text')) continue;
        const a = el.querySelector('[data-qa="chat-bubble-author-name"]');
        if (a && a.innerText.trim()) author = a.innerText.trim();
        const t = el.querySelector('[data-qa="chat-bubble-text"]');
        const text = (t ? t.innerText : el.innerText || '').trim();
        if (!text) continue;
        out.push({author, text});
    }
    return out;
}"""
BUTTONS_JS = """() => [...document.querySelectorAll('button')]
    .map(b => ({text: (b.textContent || '').trim(), qa: b.getAttribute('data-qa') || '',
                cls: b.className || ''}))
    .filter(b => b.text && b.text.length <= 80)"""
SCROLL_JS = """() => {
    const els = document.querySelectorAll('[data-qa^="chatik-open-chat-"]');
    if (els.length) els[els.length - 1].scrollIntoView({block: 'end'});
}"""
INPUT_SEL = '[data-qa="text-input"]'

AI_RE = re.compile(r"ИИ-помощник\s+присоединил|Я\s+ИИ-помощник\s+hh", re.I)
ROBOT_IN_RE = re.compile(r"Робот-рекрутер\s+присоединил", re.I)
ROBOT_OUT_RE = re.compile(r"Робот-рекрутер\s+покинул", re.I)
SERVICE_RE = re.compile(r"присоединил\w* к\s*чату|покинул\w* чат", re.I)
# вступления, которые бот клеит перед вопросом
LEAD_RE = re.compile(
    r"^(хорошо|отлично|понял\w*|спасибо|благодарю)[^.!?]*[.!]\s*|"
    r"^(следующий вопрос|ещё вопрос|еще вопрос|работодатель хотел бы узнать)\s*:\s*", re.I)

# Стандартные подсказки hh внизу любого чата: заготовки реплик работодателю,
# а не ответы боту. Встречались в чатах аккаунтов 06–12.10.2026.
GENERIC_BUTTONS = {
    "здравствуйте!", "какая схема оплаты?", "какой график работы?",
    "могу работать в гибком графике", "можно без опыта?", "у меня есть профильный опыт",
    "где находится место работы?", "готово. напишете тут по итогам?",
    "подскажите, когда ждать ответ?", "давайте продолжим в этом чате",
    "да, приду", "другое время...", "спасибо, неинтересно",
    "посмотрю вакансию, спасибо", "интересно, обсудим детали?",
    "к сожалению, не подходит", "напомнить об отклике", "добавить сопроводительное",
}


def norm(s):
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def extract_question(text):
    """Сам вопрос без вежливых вступлений: «Хорошо, учту. А расскажите…?»
    → «А расскажите…?». Иначе один вопрос с разными вступлениями
    попадал бы в банк несколько раз."""
    t = norm(text)
    while True:
        cut = LEAD_RE.sub("", t, count=1)
        if cut == t:
            return t
        t = cut.strip()


def answer_buttons(buttons):
    """Кнопки-ответы: магритте-кнопки без data-qa, кроме стандартных подсказок."""
    out = []
    for b in buttons:
        t = norm(b["text"])
        if b.get("qa") or "magritte-button" not in (b.get("cls") or ""):
            continue
        if t.lower() in GENERIC_BUTTONS or t in out:
            continue
        out.append(t)
    return out


def bot_state(msgs, buttons=()):
    """Что за бот в чате и ждёт ли он ответа.

    None — ботов нет. Иначе словарь: bot ('ai' | 'robot'), waiting, question,
    options (кнопки-ответы, только у робота).
    """
    texts = [norm(m["text"]) for m in msgs]
    ai_i = next((i for i, t in enumerate(texts) if AI_RE.search(t)), None)
    rb_i = max((i for i, t in enumerate(texts) if ROBOT_IN_RE.search(t)), default=None)
    if ai_i is None and rb_i is None:
        return None
    bot = "robot" if rb_i is not None and (ai_i is None or rb_i > ai_i) else "ai"
    last = texts[-1] if texts else ""
    waiting = last.endswith("?") and not SERVICE_RE.search(last)
    if bot == "robot" and any(ROBOT_OUT_RE.search(t) for t in texts[rb_i:]):
        waiting = False                          # робот уже вышел из чата
    return {"bot": bot, "waiting": waiting,
            "question": extract_question(last) if waiting else "",
            "options": answer_buttons(buttons) if bot == "robot" and waiting else []}


def frames(page):
    return [f for f in page.frames if "chatik" in (f.url or "")] + [page.main_frame]


def read_messages(page):
    for fr in frames(page):
        try:
            msgs = fr.evaluate(MSGS_JS)
        except Exception:
            continue
        if msgs:
            return msgs
    return []


def read_buttons(page):
    out = []
    for fr in frames(page):
        try:
            out += fr.evaluate(BUTTONS_JS)
        except Exception:
            continue
    return out


def open_chat(page, chat_id):
    page.goto(f"https://hh.ru/chat/{chat_id}", wait_until="commit")
    try:
        page.wait_for_selector('[data-qa^="chatik-chat-message-"]', timeout=8000)
        page.wait_for_timeout(800)
        return True
    except Exception:
        return False


def account(short):
    """Модули проекта этого аккаунта. Один аккаунт — один процесс: hh_autoapply
    читает .env через load_dotenv, а тот не перезаписывает уже заданные
    переменные, и второй аккаунт получил бы PROFILE_ID первого."""
    path = f"{BASE}/{ACCS[short]}"
    os.chdir(path)
    sys.path.insert(0, path)
    import hh_autoapply as hh
    return hh


def init_table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS ai_chat_pending (
        chat_id TEXT PRIMARY KEY, company TEXT, vacancy TEXT, question TEXT,
        qnorm TEXT, status TEXT, ts TEXT)""")
    cols = {r[1] for r in db.execute("PRAGMA table_info(ai_chat_pending)")}
    if "bot" not in cols:
        db.execute("ALTER TABLE ai_chat_pending ADD COLUMN bot TEXT DEFAULT 'ai'")
    if "options" not in cols:
        db.execute("ALTER TABLE ai_chat_pending ADD COLUMN options TEXT")
    # выбранные кнопки робота: ключ — вопрос и набор кнопок
    db.execute("""CREATE TABLE IF NOT EXISTS robot_choices (
        key TEXT PRIMARY KEY, question TEXT, options TEXT, choice TEXT, ts TEXT)""")
    db.commit()


def choice_key(hh, question, options):
    return hh.normalize_question(question) + " ⟦" + " | ".join(sorted(
        hh.normalize_question(o) for o in options)) + "⟧"


def known_answer(hh, db, bank, row):
    """Готовый ответ на вопрос, если он уже известен: (kind, value) или None."""
    bot, question, qn, options = row
    opts = [o for o in (options or "").split(" | ") if o]
    if opts:
        r = db.execute("SELECT choice FROM robot_choices WHERE key=?",
                       (choice_key(hh, question, opts),)).fetchone()
        return ("button", r[0]) if r else None
    return ("text", bank[qn]) if qn in bank else None


# ---------------- collect ----------------
def collect_one(short, limit):
    hh = account(short)
    import hh_chats                          # только читаем: разметку списка чатов
    from playwright.sync_api import sync_playwright
    db = hh.init_db()
    init_table(db)
    ts = dt.datetime.now().isoformat(timespec="seconds")
    rows, chats = [], []
    ws = hh.open_profile()
    try:
        with sync_playwright() as p:
            page = p.chromium.connect_over_cdp(ws).contexts[0].new_page()
            page.set_default_navigation_timeout(90000)
            page.goto("https://hh.ru/chat", wait_until="domcontentloaded")
            page.wait_for_timeout(8000)
            seen = set()
            for _ in range(10):
                for c in page.evaluate(hh_chats.SNAPSHOT_JS):
                    if c["id"] not in seen:
                        seen.add(c["id"])
                        chats.append(c)
                if len(chats) >= limit:
                    break
                page.evaluate(SCROLL_JS)
                page.wait_for_timeout(1500)
            for c in chats[:limit]:
                if not open_chat(page, c["id"]):
                    continue
                st = bot_state(read_messages(page), read_buttons(page))
                if st and st["waiting"]:
                    rows.append((c, st))
            page.close()
    finally:
        hh.close_profile()

    for c, st in rows:
        db.execute("INSERT OR REPLACE INTO ai_chat_pending "
                   "(chat_id, company, vacancy, question, qnorm, status, ts, bot, options) "
                   "VALUES (?,?,?,?,?,?,?,?,?)",
                   (c["id"], c["company"], c["vacancy"], st["question"],
                    hh.normalize_question(st["question"]), "waiting", ts, st["bot"],
                    " | ".join(st["options"]) or None))
    db.commit()
    ai = sum(1 for _, s in rows if s["bot"] == "ai")
    print(f"######## {short}: чатов просмотрено {min(len(chats), limit)}, ждут ответа: "
          f"ИИ-помощник {ai}, робот-рекрутер {len(rows) - ai}")
    for c, st in rows:
        who = "ИИ " if st["bot"] == "ai" else "РОБ"
        opts = f"  [кнопки: {' | '.join(st['options'])}]" if st["options"] else ""
        print(f"  ⚑ {who} {c['company'][:28]} | {st['question'][:100]}{opts}")


# ---------------- бланк: части 3 и 4 ----------------
PART3 = "# ЧАСТЬ 3. ИИ-ПОМОЩНИК HH В ЧАТАХ"
PART4 = "# ЧАСТЬ 4. РОБОТ-РЕКРУТЕР HH В ЧАТАХ"
BAR = "# " + "=" * 70
HEAD3 = f"""
{BAR}
{PART3} — пиши ответ после «ОТВЕТ:»
#
# Вопросы, которые ИИ-помощник задаёт в чате после отклика. Ответ уйдёт
# ему в чат. Строку ЧАТ не трогай. Не хочешь отвечать — СТАТУС: skip.
# Ответ сохранится в банк: тот же вопрос в другом чате ответится сам.
{BAR}
"""
HEAD4 = f"""
{BAR}
{PART4} — анкета работодателя в чате
#
# Есть строка ВАРИАНТЫ — это кнопки: впиши в ВЫБОР одну подпись ровно как
# в ВАРИАНТЫ, скрипт нажмёт эту кнопку. Нет вариантов — пиши ответ после
# «ОТВЕТ:», он уйдёт текстом. Строку ЧАТ не трогай. Не отвечать — skip.
{BAR}
"""


def latest_blank(short):
    files = sorted(Path(f"{BASE}/{ACCS[short]}/questions/new").glob("*.txt"),
                   key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def strip_parts(text):
    """Убрать из бланка старые части 3 и 4, если они там уже есть."""
    cut = [text.rfind(BAR, 0, i) for i in (text.find(PART3), text.find(PART4)) if i >= 0]
    cut = [c for c in cut if c >= 0]
    return (text[:min(cut)] if cut else text).rstrip() + "\n"


def export_one(short):
    hh = account(short)
    db = hh.init_db()
    init_table(db)
    bank = hh.load_answer_bank(db)
    rows = db.execute(
        "SELECT chat_id, company, vacancy, question, qnorm, bot, options "
        "FROM ai_chat_pending WHERE status='waiting' ORDER BY ts").fetchall()
    rows = [r for r in rows if not known_answer(hh, db, bank, (r[5], r[3], r[4], r[6]))]
    path = latest_blank(short)
    if path is None:
        print(f"######## {short}: бланка нет — сначала questions.sh export")
        return
    text = strip_parts(path.read_text(encoding="utf-8"))
    ai = [r for r in rows if r[5] != "robot"]
    rb = [r for r in rows if r[5] == "robot"]
    if ai:
        text += HEAD3 + "\n" + "".join(
            f"---\nЧАТ: {cid}\nСТАТУС: needs_input\nКОМПАНИЯ: {comp}\n"
            f"ВАКАНСИЯ: {vac}\nВОПРОС: {q}\nОТВЕТ:\n\n"
            for cid, comp, vac, q, *_ in ai) + "---\n"
    if rb:
        blocks = []
        for cid, comp, vac, q, _, _, opts in rb:
            b = (f"---\nЧАТ: {cid}\nСТАТУС: needs_input\nКОМПАНИЯ: {comp}\n"
                 f"ВАКАНСИЯ: {vac}\nВОПРОС: {q}\n")
            b += (f"ВАРИАНТЫ: {opts}\nВЫБОР: \n" if opts else "ОТВЕТ:\n\n")
            blocks.append(b)
        text += HEAD4 + "\n" + "".join(blocks) + "---\n"
    path.write_text(text, encoding="utf-8")
    print(f"######## {short}: в {path.name} ИИ-помощник {len(ai)}, робот-рекрутер {len(rb)}")
    for r in rows:
        print(f"  ⚑ {'ИИ ' if r[5] != 'robot' else 'РОБ'} {r[1][:28]} | {r[3][:100]}"
              + (f"  [{r[6]}]" if r[6] else ""))


def take_one(short, dry):
    """Забрать ответы частей 3 и 4 и отправить их в чаты."""
    hh = account(short)
    from hh_answers import _parse_status
    db = hh.init_db()
    init_table(db)
    path = latest_blank(short)
    ts = dt.datetime.now().isoformat(timespec="seconds")
    got = skipped = bad = 0
    if path is not None:
        for block in re.split(r"(?m)^---\s*$", path.read_text(encoding="utf-8")):
            m = re.search(r"(?m)^ЧАТ:\s*(\S+)", block)
            if not m:
                continue
            cid = m.group(1)
            row = db.execute("SELECT question, qnorm, options FROM ai_chat_pending "
                             "WHERE chat_id=?", (cid,)).fetchone()
            if not row:
                continue
            st = _parse_status((re.search(r"(?m)^СТАТУС:\s*(.*)$", block) or [None, ""])[1])
            if st == "skip":
                db.execute("UPDATE ai_chat_pending SET status='skip' WHERE chat_id=?", (cid,))
                skipped += 1
                continue
            if st != "approved":
                continue
            opts = [o for o in (row[2] or "").split(" | ") if o]
            if opts:
                pick = norm((re.search(r"(?m)^ВЫБОР:\s*(.*)$", block) or [None, ""])[1])
                if pick not in opts:
                    print(f"  ✗ чат {cid}: ВЫБОР «{pick}» не из вариантов {opts}")
                    bad += 1
                    continue
                db.execute("INSERT OR REPLACE INTO robot_choices VALUES (?,?,?,?,?)",
                           (choice_key(hh, row[0], opts), row[0], row[2], pick, ts))
                got += 1
                continue
            ans = block.split("ОТВЕТ:", 1)[1] if "ОТВЕТ:" in block else ""
            ans = "\n".join(l for l in ans.splitlines() if not l.lstrip().startswith("#")).strip()
            if not ans:
                continue
            if db.execute("SELECT 1 FROM answer_bank WHERE qnorm=?", (row[1],)).fetchone():
                db.execute("UPDATE answer_bank SET answer=?, status='approved', ts=? "
                           "WHERE qnorm=?", (ans, ts, row[1]))
            else:
                db.execute("INSERT INTO answer_bank (qnorm, question, answer, status, ts, kind)"
                           " VALUES (?,?,?,?,?,?)", (row[1], row[0], ans, "approved", ts,
                                                     "textarea"))
            got += 1
        db.commit()
    print(f"######## {short}: из бланка принято {got}, skip {skipped}, с ошибкой {bad}")
    reply_one(short, dry, hh=hh)


# ---------------- reply ----------------
def reply_one(short, dry, hh=None):
    hh = hh or account(short)
    from playwright.sync_api import sync_playwright
    db = hh.init_db()
    init_table(db)
    bank = hh.load_answer_bank(db)
    todo = []
    for cid, comp, q, qn, bot, opts in db.execute(
            "SELECT chat_id, company, question, qnorm, bot, options FROM ai_chat_pending "
            "WHERE status='waiting'").fetchall():
        k = known_answer(hh, db, bank, (bot, q, qn, opts))
        if k:
            todo.append((cid, comp, q, k))
    print(f"######## {short}: ответов к отправке {len(todo)}" + ("  [сухой]" if dry else ""))
    if not todo:
        return
    ws = hh.open_profile()
    try:
        with sync_playwright() as p:
            page = p.chromium.connect_over_cdp(ws).contexts[0].new_page()
            page.set_default_navigation_timeout(90000)
            for cid, comp, q, (kind, value) in todo:
                if not open_chat(page, cid):
                    print(f"  ✗ {comp[:30]}: чат не открылся")
                    continue
                st = bot_state(read_messages(page), read_buttons(page))
                # отвечаем, только если бот всё ещё ждёт ровно этого
                if (not st or not st["waiting"]
                        or hh.normalize_question(st["question"]) != hh.normalize_question(q)):
                    db.execute("UPDATE ai_chat_pending SET status='stale' WHERE chat_id=?", (cid,))
                    db.commit()
                    print(f"  – {comp[:30]}: вопрос уже другой или ответ дан, пропускаю")
                    continue
                if dry:
                    how = "нажал бы кнопку" if kind == "button" else "напечатал бы"
                    print(f"  [сухой] {comp[:30]} | {q[:60]} → {how}: {value[:90]}")
                    continue
                if kind == "button":
                    if value not in st["options"]:
                        print(f"  ✗ {comp[:30]}: кнопки «{value}» на странице нет")
                        continue
                    sent = False
                    for fr in frames(page):
                        btn = fr.get_by_role("button", name=value, exact=True)
                        if btn.count():
                            btn.first.click()
                            sent = True
                            break
                    if not sent:
                        print(f"  ✗ {comp[:30]}: кнопка не нажалась")
                        continue
                else:
                    field = page.locator(INPUT_SEL)
                    if field.count() == 0 or not field.first.is_visible():
                        print(f"  ✗ {comp[:30]}: нет поля ввода")
                        continue
                    field.first.click()
                    field.first.fill(value)
                    hh.pause(1, 2)
                    page.keyboard.press("Enter")
                hh.pause(3, 5)
                db.execute("UPDATE ai_chat_pending SET status='sent', ts=? WHERE chat_id=?",
                           (dt.datetime.now().isoformat(timespec="seconds"), cid))
                db.commit()
                print(f"  ✓ {comp[:30]} | {q[:60]} → {value[:90]}")
            page.close()
    finally:
        db.commit()
        hh.close_profile()


# ---------------- peek ----------------
def peek(short, wants):
    hh = account(short)
    from playwright.sync_api import sync_playwright
    ws = hh.open_profile()
    try:
        with sync_playwright() as p:
            page = p.chromium.connect_over_cdp(ws).contexts[0].new_page()
            page.goto("https://hh.ru/chat", wait_until="domcontentloaded")
            page.wait_for_timeout(6000)
            for w in wants:
                if w.isdigit():                  # номер чата — открываем напрямую
                    ok = open_chat(page, w)
                    page.wait_for_timeout(2000)
                    print(f"\n######## чат {w}: {'открылся' if ok else 'не открылся'}")
                    if not ok:
                        continue
                else:
                    item = page.get_by_text(re.compile(w, re.I)).first
                    print(f"\n######## {w}: {'нашёлся' if item.count() else 'в списке чатов НЕТ'}")
                    if not item.count():
                        continue
                    item.click()
                    page.wait_for_timeout(5000)
                msgs, buttons = read_messages(page), read_buttons(page)
                for m in msgs:
                    print(f"  [{m['author'] or '—'}] " + norm(m["text"])[:400])
                print("  кнопки-ответы:", answer_buttons(buttons) or "нет")
                print("  состояние:", bot_state(msgs, buttons))
            page.close()
    finally:
        hh.close_profile()


def each_account(*args):
    for short in ACCS:
        subprocess.run([sys.executable, __file__, *args[:1], short, *args[1:]])


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["collect"] and (len(a) < 2 or a[1].isdigit()):
        each_account("collect-one", a[1] if len(a) > 1 else "30")
    elif a[:1] == ["collect-one"] and len(a) == 3 and a[1] in ACCS:
        collect_one(a[1], int(a[2]))
    elif a[:1] == ["export"]:
        each_account("export-one")
    elif a[:1] == ["export-one"] and len(a) == 2 and a[1] in ACCS:
        export_one(a[1])
    elif a[:1] == ["take"] and (len(a) < 2 or a[1] == "--dry"):
        each_account("take-one", *(a[1:2]))
    elif a[:1] == ["take-one"] and len(a) >= 2 and a[1] in ACCS:
        take_one(a[1], "--dry" in a)
    elif a[:1] == ["reply"] and (len(a) < 2 or a[1] == "--dry"):
        each_account("reply-one", *(a[1:2]))
    elif a[:1] == ["reply-one"] and len(a) >= 2 and a[1] in ACCS:
        reply_one(a[1], "--dry" in a)
    elif a[:1] == ["peek"] and len(a) >= 3 and a[1] in ACCS:
        peek(a[1], a[2:])
    else:
        print(__doc__)
