"""Вопросы ИИ-помощника hh из чатов — в общий бланк ответов, ответы — обратно в чат.

    python scripts/ai_chats.py collect [N]
        пройти N свежих чатов (по умолчанию 30) на всех трёх аккаунтах; где
        ИИ-помощник ждёт ответа, занести его вопрос в банк ответов. Дальше он
        выгружается обычным questions.sh export с пометкой в строке КОМПАНИЯ
        «ИИ-помощник в чате». Вопрос, на который в банке уже есть одобренный
        ответ, в бланк не попадёт — на него reply ответит сразу.
    python scripts/ai_chats.py reply [--dry]
        отправить одобренные ответы в чаты, где помощник всё ещё ждёт именно
        этого ответа. --dry только показывает, что было бы отправлено.
    python scripts/ai_chats.py peek <hh|hh2|hh3> <часть названия компании> ...
        прочитать конкретные чаты целиком

Как узнать ИИ-помощника. Имени автора у его сообщений нет, и hh помечает их
так же, как собственные, поэтому по автору его не отличить. Узнаём по
тексту: служебная строка «ИИ-помощник присоединился к чату» и первое
сообщение «Я ИИ-помощник hh». Робот-рекрутер — другое: он пишет
«Робот-рекрутер присоединился» и задаёт анкету с кнопками; его не трогаем.

Помощник задаёт вопросы по одному: на ответ он присылает следующий. Цикл
поэтому повторяется: collect → export → ответил → take → reply → collect…

Открытие чата снимает у него отметку «непрочитано». Работодатель этого не
видит, счётчик непрочитанных только твой.
"""
import datetime as dt
import os
import re
import subprocess
import sys

BASE = "/Users/mr_macbook/Documents"
ACCS = {"hh": "hh_autoreply", "hh2": "hh_autoreply_2", "hh3": "hh_autoreply_3"}
MARK = "ИИ-помощник в чате"

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
        out.push({author, own: !!el.querySelector('[data-qa="chat-bubble-indicators"]'), text});
    }
    return out;
}"""
SCROLL_JS = """() => {
    const els = document.querySelectorAll('[data-qa^="chatik-open-chat-"]');
    if (els.length) els[els.length - 1].scrollIntoView({block: 'end'});
}"""
INPUT_SEL = '[data-qa="text-input"]'

AI_RE = re.compile(r"ИИ-помощник\s+присоединил|Я\s+ИИ-помощник\s+hh", re.I)
# вступления, которые помощник клеит перед вопросом
LEAD_RE = re.compile(
    r"^(хорошо|отлично|понял\w*|спасибо|благодарю)[^.!?]*[.!]\s*|"
    r"^(следующий вопрос|ещё вопрос|еще вопрос|работодатель хотел бы узнать)\s*:\s*", re.I)


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


def ai_state(msgs):
    """None — помощника в чате нет. Иначе (ждёт ли ответа, вопрос)."""
    texts = [norm(m["text"]) for m in msgs]
    start = next((i for i, t in enumerate(texts) if AI_RE.search(t)), None)
    if start is None:
        return None
    last = texts[-1] if texts else ""
    waiting = last.endswith("?") and not AI_RE.search(last)
    return waiting, extract_question(last) if waiting else ""


def read_messages(page):
    for fr in [f for f in page.frames if "chatik" in (f.url or "")] + [page.main_frame]:
        try:
            msgs = fr.evaluate(MSGS_JS)
        except Exception:
            continue
        if msgs:
            return msgs
    return []


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
    db.commit()


# ---------------- collect ----------------
def collect_one(short, limit):
    hh = account(short)
    import hh_chats                          # только читаем: разметку списка чатов
    from playwright.sync_api import sync_playwright
    db = hh.init_db()
    init_table(db)
    bank = hh.load_answer_bank(db)
    ts = dt.datetime.now().isoformat(timespec="seconds")
    rows = []
    ws = hh.open_profile()
    try:
        with sync_playwright() as p:
            page = p.chromium.connect_over_cdp(ws).contexts[0].new_page()
            page.set_default_navigation_timeout(90000)
            page.goto("https://hh.ru/chat", wait_until="domcontentloaded")
            page.wait_for_timeout(8000)
            chats, seen = [], set()
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
                st = ai_state(read_messages(page))
                if st and st[0]:
                    rows.append((c, st[1]))
            page.close()
    finally:
        hh.close_profile()

    new = known = 0
    for c, q in rows:
        qn = hh.normalize_question(q)
        db.execute("INSERT OR REPLACE INTO ai_chat_pending VALUES (?,?,?,?,?,?,?)",
                   (c["id"], c["company"], c["vacancy"], q, qn, "waiting", ts))
        if qn in bank:
            known += 1
            continue
        # В пул вопросов — ради строки КОМПАНИЯ в бланке; в банк — чтобы
        # вопрос выгрузился обычным export как текстовый.
        db.execute("INSERT OR REPLACE INTO questions VALUES (?,?,?,?,?,?,?,?)",
                   (f"chat:{c['id']}", f"https://hh.ru/chat/{c['id']}",
                    f"{c['company']} · {MARK}", 0, q, "textarea", "", ts))
        if not db.execute("SELECT 1 FROM answer_bank WHERE qnorm=?", (qn,)).fetchone():
            db.execute("INSERT INTO answer_bank (qnorm, question, answer, status, ts, kind) "
                       "VALUES (?,?,?,?,?,?)", (qn, q, "", "needs_input", ts, "textarea"))
        new += 1
    db.commit()
    print(f"######## {short}: чатов просмотрено {min(len(chats), limit)}, "
          f"помощник ждёт ответа в {len(rows)}: в бланк {new}, уже есть ответ {known}")
    for c, q in rows:
        print(f"  ⚑ {c['company'][:30]} | {c['vacancy'][:40]} | {q[:120]}")


# ---------------- reply ----------------
def reply_one(short, dry):
    hh = account(short)
    from playwright.sync_api import sync_playwright
    db = hh.init_db()
    init_table(db)
    bank = hh.load_answer_bank(db)
    todo = [(cid, comp, q, bank[qn]) for cid, comp, q, qn in db.execute(
        "SELECT chat_id, company, question, qnorm FROM ai_chat_pending WHERE status='waiting'")
        if qn in bank]
    print(f"######## {short}: ответов к отправке {len(todo)}" + ("  [сухой]" if dry else ""))
    if not todo:
        return
    ws = hh.open_profile()
    try:
        with sync_playwright() as p:
            page = p.chromium.connect_over_cdp(ws).contexts[0].new_page()
            page.set_default_navigation_timeout(90000)
            for cid, comp, q, answer in todo:
                if not open_chat(page, cid):
                    print(f"  ✗ {comp[:30]}: чат не открылся")
                    continue
                st = ai_state(read_messages(page))
                # отвечаем, только если помощник всё ещё ждёт ровно этого
                if not st or not st[0] or hh.normalize_question(st[1]) != hh.normalize_question(q):
                    db.execute("UPDATE ai_chat_pending SET status='stale' WHERE chat_id=?", (cid,))
                    print(f"  – {comp[:30]}: вопрос уже другой или ответ дан, пропускаю")
                    continue
                field = page.locator(INPUT_SEL)
                if field.count() == 0 or not field.first.is_visible():
                    print(f"  ✗ {comp[:30]}: нет поля ввода")
                    continue
                if dry:
                    print(f"  [сухой] {comp[:30]} | {q[:70]} → {answer[:90]}")
                    continue
                field.first.click()
                field.first.fill(answer)
                hh.pause(1, 2)
                page.keyboard.press("Enter")
                hh.pause(3, 5)
                db.execute("UPDATE ai_chat_pending SET status='sent', "
                           "ts=? WHERE chat_id=?",
                           (dt.datetime.now().isoformat(timespec="seconds"), cid))
                db.commit()
                print(f"  ✓ {comp[:30]} | {q[:70]} → {answer[:90]}")
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
                item = page.get_by_text(re.compile(w, re.I)).first
                print(f"\n######## {w}: {'нашёлся' if item.count() else 'в списке чатов НЕТ'}")
                if not item.count():
                    continue
                item.click()
                page.wait_for_timeout(5000)
                for m in read_messages(page):
                    print(f"  [{m['author'] or '—'}] " + norm(m["text"])[:400])
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
    elif a[:1] == ["reply"] and (len(a) < 2 or a[1] == "--dry"):
        each_account("reply-one", *(a[1:2]))
    elif a[:1] == ["reply-one"] and len(a) >= 2 and a[1] in ACCS:
        reply_one(a[1], "--dry" in a)
    elif a[:1] == ["peek"] and len(a) >= 3 and a[1] in ACCS:
        peek(a[1], a[2:])
    else:
        print(__doc__)
