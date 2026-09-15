"""
Чаты с работодателями на hh: сбор вопросов робота-рекрутера.

Это отдельная от анкет вакансий история, поэтому и таблицы свои:
chat_questions и chat_answers. В анкете вакансии свободный ввод,
в чате — диалог с готовыми вариантами ответа.

    venv/bin/python hh_chats.py --scan        обойти чаты и собрать вопросы
    venv/bin/python hh_chats.py --scan 5      только первые 5 чатов
    venv/bin/python hh_chats.py --list        показать собранное
    venv/bin/python hh_chats.py               сводка

ВАЖНО: чтобы прочитать вопросы, чат приходится открыть, а открытие снимает
отметку «непрочитано». Сообщения при этом никуда не деваются, и работодатель
этого не видит — счётчик непрочитанных твой личный.

Сканер ничего не нажимает и не отправляет.
"""
import datetime as dt
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import hh_autoapply as hh
from hh_autoapply import init_db, normalize_question
from playwright.sync_api import sync_playwright

CHAT_LIST = "https://hh.ru/chat"
# Собеседники, чьи реплики имеет смысл собирать. Своё имя сюда не попадает:
# автор указывается только у входящих сообщений.
# Кнопки внизу чата — НЕ варианты ответа: у робота-рекрутера это действительно
# ответы («Важна / Неважна»), а у ИИ-помощника и живого HR — заготовки вопросов
# к работодателю («Какой график работы?»). Различить автоматически нельзя,
# поэтому просто сохраняем их как есть, не трактуя.
SKIP_AUTHORS = ("Бот-помощник Хэдди",)

SEL = {
    "cell": '[data-qa^="chatik-open-chat-"]',
    "title": '[data-qa="chat-cell-title"]',
    "subtitle": '[data-qa="chat-cell-subtitle"]',
    "badge": '[data-qa="chatik-info-badges"]',
    "input": '[data-qa="chatik-message-input"]',
}


SNAPSHOT_JS = """() => {
    const out = [];
    for (const el of document.querySelectorAll('[data-qa^="chatik-open-chat-"]')) {
        const q = s => { const n = el.querySelector(s); return n ? (n.innerText||'').trim() : ''; };
        out.push({
            id: (el.getAttribute('data-qa')||'').replace('chatik-open-chat-',''),
            vacancy: q('[data-qa="chat-cell-title"]'),
            company: q('[data-qa="chat-cell-subtitle"]'),
            unread: q('[data-qa="chatik-info-badges"]'),
        });
    }
    return out;
}"""


def list_chats(page):
    """Список чатов.

    Список виртуализированный: в DOM живут только видимые строки, поэтому
    собираем их по ходу прокрутки, а не одним снимком в конце — иначе
    получим последнее окно вместо всего списка.
    """
    page.goto(CHAT_LIST, wait_until="commit")
    page.wait_for_timeout(9000)

    found, stale = {}, 0
    for _ in range(60):
        before = len(found)
        for c in page.evaluate(SNAPSHOT_JS):
            found.setdefault(c["id"], c)
        stale = stale + 1 if len(found) == before else 0
        if stale >= 4:
            break
        page.evaluate("""() => {
            const els = document.querySelectorAll('[data-qa^="chatik-open-chat-"]');
            if (els.length) els[els.length - 1].scrollIntoView({block: 'end'});
        }""")
        page.wait_for_timeout(1500)

    return list(found.values())


def read_chat(page, chat_id):
    """Сообщения робота и текущие варианты ответа. Ничего не нажимает."""
    page.goto(f"https://hh.ru/chat/{chat_id}", wait_until="commit")
    page.wait_for_timeout(6000)

    # Имя автора hh показывает только у первого сообщения серии, поэтому
    # тянем последнего известного автора вперёд.
    msgs = page.evaluate("""() => {
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
            out.push({ id: qa.replace('chatik-chat-message-',''), author, text });
        }
        return out;
    }""")

    buttons = page.evaluate("""() => [...document.querySelectorAll('button')]
        .filter(b => !b.getAttribute('data-qa'))
        .map(b => (b.textContent||'').trim())
        .filter(t => t && t.length <= 60)""")
    return msgs, buttons


# служебные реплики, на которые отвечать нечего
NOISE_RE = re.compile(
    r"^нач[нё]м\s*\?$|присоединил\w* к\s*чату|без сопроводительного"
    r"|добавить сопроводительное|ответьте на приглашение"
    # приглашения пройти интервью на внешней платформе: вопроса тут нет,
    # «?» приезжает из ссылки
    r"|интервью с\s*гигарекрутером|не забудьте пройти|пройдите коротк", re.I)


def is_question(text):
    """Похоже на вопрос собеседника, а не на служебную реплику."""
    t = re.sub(r"\s+", " ", text).strip()
    if len(t) < 12 or "?" not in t:
        return False
    return not NOISE_RE.search(t)


def scan(limit=None):
    db = init_db()
    if not hh.PROFILE_ID:
        raise SystemExit("PROFILE_ID не задан, заполни .env")

    ws = hh.open_profile()
    saved, chats_done = 0, 0
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(ws)
        ctx = browser.contexts[0]
        page = ctx.new_page()
        page.set_default_navigation_timeout(90000)
        try:
            chats = list_chats(page)
            print(f"чатов в списке: {len(chats)}")
            if limit:
                chats = chats[:limit]
                print(f"ограничение: обхожу первые {len(chats)}")

            for c in chats:
                try:
                    msgs, buttons = read_chat(page, c["id"])
                except Exception as e:
                    print(f"  [{c['id']}] ошибка: {type(e).__name__}: {str(e)[:90]}")
                    continue
                chats_done += 1
                questions = [m for m in msgs
                             if is_question(m["text"])
                             and m["author"] not in SKIP_AUTHORS]
                for m in questions:
                    # кнопки висят внизу чата и относятся к его текущему
                    # состоянию, поэтому пишем их только последнему вопросу
                    btns = " | ".join(buttons) if m is questions[-1] else ""
                    db.execute(
                        "INSERT OR REPLACE INTO chat_questions VALUES (?,?,?,?,?,?,?,?)",
                        (c["id"], m["id"], c["company"], c["vacancy"],
                         m["author"], m["text"].strip(), btns,
                         dt.datetime.now().isoformat(timespec="seconds")))
                    saved += 1
                db.commit()
                flag = f" (непрочитано {c['unread']})" if c["unread"] else ""
                print(f"  [{c['id']}] {c['company'][:28]:28s} "
                      f"вопросов: {len(questions)}{flag}")
                hh.pause(2, 4)
        finally:
            page.close()
            hh.close_profile()

    print(f"\nобойдено чатов: {chats_done}, сохранено вопросов: {saved}")
    stats(db)


def show(db):
    rows = db.execute(
        "SELECT company, vacancy, author, question, buttons FROM chat_questions "
        "ORDER BY company").fetchall()
    for company, vacancy, author, question, buttons in rows:
        flat = re.sub(r"\s+", " ", question).strip()
        print(f"\n  {company} — {vacancy[:50]}")
        print(f"    [{author or '—'}] {flat[:150]}")
        if buttons:
            print(f"    кнопки внизу чата: {buttons}")


def stats(db=None):
    db = db or init_db()
    total = db.execute("SELECT COUNT(*) FROM chat_questions").fetchone()[0]
    chats = db.execute(
        "SELECT COUNT(DISTINCT chat_id) FROM chat_questions").fetchone()[0]
    uniq = len({normalize_question(q) for (q,) in
                db.execute("SELECT question FROM chat_questions")})
    print(f"\nвопросов из чатов: {total} | чатов: {chats} | уникальных: {uniq}")
    print("\nпо авторам:")
    for author, n in db.execute(
            "SELECT author, COUNT(*) FROM chat_questions "
            "GROUP BY author ORDER BY 2 DESC"):
        print(f"  {n:3d}  {author or '—'}")

    print("\nсамые частые формулировки:")
    counts = {}
    for (q,) in db.execute("SELECT question FROM chat_questions"):
        qn = normalize_question(q)
        counts[qn] = counts.get(qn, 0) + 1
    for qn, n in sorted(counts.items(), key=lambda x: -x[1])[:10]:
        print(f"  {n}x  {qn[:95]}")


def main():
    args = sys.argv[1:]
    if "--scan" in args:
        i = args.index("--scan")
        limit = int(args[i + 1]) if len(args) > i + 1 and args[i + 1].isdigit() else None
        scan(limit)
    elif "--list" in args:
        show(init_db())
    else:
        stats()


if __name__ == "__main__":
    main()
