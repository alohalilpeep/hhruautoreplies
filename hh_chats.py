"""
Чаты с работодателями на hh: сбор, разметка и отчёт «куда заглянуть».

Это отдельная от анкет вакансий история, поэтому и таблицы свои. В анкете
вакансии свободный ввод, в чате — живой диалог. В базу попадают все
сообщения (chat_messages), а не только вопросы: отказы и приглашения нужны
для статистики не меньше.

    venv/bin/python hh_chats.py --attention    куда стоит заглянуть самому
    venv/bin/python hh_chats.py --scan --new   добрать новые и непрочитанное
    venv/bin/python hh_chats.py --scan         полный обход, нужен один раз
    venv/bin/python hh_chats.py --scan 5       только первые 5 чатов
    venv/bin/python hh_chats.py --list         показать собранные вопросы
    venv/bin/python hh_chats.py --fill         завести ответы под новые вопросы
    venv/bin/python hh_chats.py --export       выгрузить в chat_answers_edit.txt
    venv/bin/python hh_chats.py --import       вернуть правки в базу
    venv/bin/python hh_chats.py --reply        СУХОЙ прогон: что бы отправил
    venv/bin/python hh_chats.py --reply --send БОЕВОЙ: реально отправляет
    venv/bin/python hh_chats.py                сводка

ВАЖНО: чтобы прочитать чат, его приходится открыть, а открытие снимает
отметку «непрочитано». Сообщения при этом никуда не деваются, и работодатель
этого не видит — счётчик непрочитанных твой личный.

Обратная сторона: отметке hh верить нельзя. Чат, открытый тобой руками,
выглядит для скрипта пустым, поэтому --new опирается на нашу базу («этот чат
мы уже читали»), а не на счётчик непрочитанных.

Сканер ничего не нажимает и не отправляет. Отправляет только --reply --send,
и только ответы со статусом approved.
"""
import datetime as dt
import re
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import hh_autoapply as hh
from hh_autoapply import init_db, normalize_question
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

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

    # Ограничение на число прокруток — предохранитель от бесконечного цикла,
    # а не способ остановиться: выход по stale, когда список перестал расти.
    # При 60 итерациях обход упирался в потолок ровно на 300 чатах и молча
    # терял хвост списка, поэтому запас с большим запасом.
    found, stale = {}, 0
    for _ in range(400):
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
    # Раньше тут стояло глухое ожидание в 6 секунд на каждый чат. Обычно
    # сообщения появляются гораздо раньше, поэтому ждём их саму разметку,
    # а фиксированный таймаут оставляем только как запасной путь.
    try:
        page.wait_for_selector('[data-qa^="chatik-chat-message-"]',
                               timeout=8000)
        page.wait_for_timeout(600)   # добрать хвост серии сообщений
    except PWTimeout:
        page.wait_for_timeout(1500)  # пустой чат или медленная отдача

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


# Тип сообщения. Порядок проверок важен: отказ распознаём раньше
# приглашения, иначе «к сожалению, не готовы пригласить» уедет в приглашения.
REJECT_RE = re.compile(
    r"к сожалению|вынужден\w* отказ|отказ\w* от|не готовы (пригласить|продолж)"
    r"|не подходит\w*|не подошли|выбрали другого|не будем продолжать"
    r"|приостановил\w* подбор|закрыл\w* вакансию", re.I)
# Шаблонная отписка сразу после отклика. Это НЕ интерес: «мы свяжемся с
# вами» означает ровно обратное — пока ничего не происходит.
AUTO_RE = re.compile(
    r"рассмотрим ваше резюме|если навыки и опыт подойдут|мы свяжемся с вами"
    r"|благодарим (вас )?за (отклик|интерес|участие)|спасибо за отклик"
    r"|спасибо, что откликнулись|внимательно ознакомимся"
    r"|в случае положительного", re.I)

# Автоматический скрининг: робот или тест на внешней площадке. hh считает
# это собеседованием, живого человека здесь нет.
SCREEN_RE = re.compile(
    r"гигарекрутер|первичное интервью|пройдите коротк|пройти тест"
    r"|видеоинтервью|бот\w* в telegram|ссылк\w* на бот", re.I)

# Настоящее приглашение: зовут говорить и согласуют время.
INVITE_RE = re.compile(
    r"пригла\w+ (вас )?на (собеседован|интервью|встреч|созвон|знакомств)"
    r"|когда вам удобно|удобно ли вам|давайте созвон|назначим (встреч|созвон|звонок)"
    r"|готовы обсудить|предлагаю созвон|техническ\w* секци"
    r"|знакомств\w* с командой", re.I)


def classify_message(text, author=""):
    """Разметка сообщения. Порядок проверок — это и есть вся логика.

    Отказ важнее приглашения («к сожалению, не готовы пригласить»), а
    шаблонная отписка важнее и того и другого: «мы свяжемся с вами» ловится
    почти любой регуляркой про интерес, хотя интереса там нет.
    """
    t = re.sub(r"\s+", " ", text or "").strip()
    if author in SKIP_AUTHORS:
        return "бот"
    if REJECT_RE.search(t):
        return "отказ"
    # Живой вопрос важнее вежливого вступления: HR часто начинает с
    # «Спасибо за отклик!», а дальше идут настоящие вопросы, и по шаблону
    # такое письмо уезжало в автоответы.
    if is_question(t):
        return "вопрос"
    if AUTO_RE.search(t):
        return "автоответ"
    if SCREEN_RE.search(t):
        return "скрининг"
    if INVITE_RE.search(t):
        return "приглашение"
    return "инфо"


def is_question(text):
    """Похоже на вопрос собеседника, а не на служебную реплику."""
    t = re.sub(r"\s+", " ", text).strip()
    if len(t) < 12 or "?" not in t:
        return False
    return not NOISE_RE.search(t)


# Наши же сообщения и элементы интерфейса, попавшие в выдачу как реплики.
# hh не помечает автора у исходящих, поэтому опознаём по началу письма.
OWN_RE = re.compile(r"^Добрый день, заинтересовала", re.I)
UI_RE = re.compile(
    r"^(без сопроводительного|добавить сопроводительное|начн[её]м)", re.I)
# Зовут поговорить, но словами, под которые шаблон не подгонишь
LEAD_RE = re.compile(
    r"интересное резюме|хотели бы.{0,30}(связат|обсуд)|позвоните|наберите"
    r"|предлагаю (созвон|встреч|пообщ)|когда (вам )?(будет )?удобно"
    r"|давайте (созвон|обсуд|пообщ)|напишите в чат|свяжитесь", re.I)


def attention(db=None):
    """Чаты, куда стоит заглянуть самому. Ничего не отправляет.

    Берём те, где последнее слово осталось за работодателем и это вопрос
    или приглашение. Чаты с отказом пропускаем: смотреть там нечего.
    """
    db = db or init_db()
    chats = {}
    for cid, comp, vac, auth, text, kind in db.execute(
            "SELECT chat_id, company, vacancy, author, text, kind "
            "FROM chat_messages ORDER BY rowid"):
        flat = re.sub(r"\s+", " ", text or "").strip()
        if OWN_RE.match(flat) or UI_RE.match(flat):
            continue
        chats.setdefault(cid, {"c": comp, "v": vac, "m": []})
        chats[cid]["m"].append((kind, auth, flat))

    need = []
    for cid, d in chats.items():
        if any(k == "отказ" for k, _, _ in d["m"]):
            continue
        kind, auth, text = d["m"][-1]
        lead = bool(LEAD_RE.search(text))
        if kind in ("вопрос", "приглашение") or lead:
            need.append((cid, d, "приглашение" if lead else kind, auth, text))
    need.sort(key=lambda x: 0 if x[2] == "приглашение" else 1)

    print(f"\n=== стоит заглянуть: {len(need)} чатов\n")
    for cid, d, kind, auth, text in need:
        print(f"[{kind.upper()}] {d['c'][:34]} — {d['v'][:44]}")
        print(f"   {auth or '—'}: {text[:170]}")
        print(f"   https://hh.ru/chat/{cid}\n")
    return need


def scan(limit=None, only_new=False):
    db = init_db()
    if not hh.PROFILE_ID:
        raise SystemExit("PROFILE_ID не задан, заполни .env")

    ws = hh.open_profile()
    saved, chats_done, kept = 0, 0, 0
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(ws)
        ctx = browser.contexts[0]
        page = ctx.new_page()
        page.set_default_navigation_timeout(90000)
        try:
            chats = list_chats(page)
            print(f"чатов в списке: {len(chats)}")
            if only_new:
                # Полный обход трёхсот чатов занимает около часа. В чат, где
                # мы уже были и нового не появилось, заходить незачем:
                # берём непрочитанные и те, которых ещё нет в базе.
                seen = {r[0] for r in db.execute(
                    "SELECT DISTINCT chat_id FROM chat_messages")}
                total = len(chats)
                chats = [c for c in chats
                         if c["id"] not in seen or c["unread"]]
                print(f"из них новых или с непрочитанным: {len(chats)} "
                      f"(пропускаю {total - len(chats)})")
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
                # сначала складываем всё, что написали: отказы и приглашения
                # нужны для статистики не меньше вопросов
                for m in msgs:
                    db.execute(
                        "INSERT OR REPLACE INTO chat_messages "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (c["id"], m["id"], c["company"], c["vacancy"],
                         m["author"], m["text"].strip(),
                         classify_message(m["text"], m["author"]),
                         dt.datetime.now().isoformat(timespec="seconds")))
                    kept += 1
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
                # Пауза между чатами короче, чем в прогоне откликов: здесь
                # мы только читаем и ничего не отправляем.
                hh.pause(0.6, 1.4)
        finally:
            page.close()
            hh.close_profile()

    print(f"\nобойдено чатов: {chats_done}, сохранено сообщений: {kept}, из них вопросов: {saved}")
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
    if "--fill" in args:
        fill_answers(init_db())
    elif "--export" in args:
        export_answers(init_db())
    elif "--import" in args:
        import_answers(init_db())
    elif "--reply" in args:
        i = args.index("--reply")
        lim = int(args[i + 1]) if len(args) > i + 1 and args[i + 1].isdigit() else None
        reply(lim, dry="--send" not in args)
    elif "--scan" in args:
        i = args.index("--scan")
        limit = int(args[i + 1]) if len(args) > i + 1 and args[i + 1].isdigit() else None
        scan(limit, only_new="--new" in args)
    elif "--attention" in args:
        attention()
    elif "--list" in args:
        show(init_db())
    else:
        stats()



# ============ банк ответов для чатов ============
# Отдельный от анкет вакансий: там свободная форма, здесь диалог.
from hh_answers import qid, _parse_status, SEP  # noqa: E402

EDIT_FILE = Path(__file__).with_name("chat_answers_edit.txt")

CHAT_HEADER = """\
# Ответы на вопросы из чатов с работодателями. Экспорт из hh_responses.db.
#
# Правь ТОЛЬКО текст после «ОТВЕТ:» и, если нужно, строку «СТАТУС:».
# Строку «ID:» не трогай. Блоки разделены строкой из трёх дефисов.
#
# КНОПКИ — то, что hh показывает внизу чата. У робота-рекрутера это варианты
# ответа, у ИИ-помощника и живого HR — заготовки вопросов К работодателю.
# Если ответ точно совпадает с текстом кнопки, скрипт нажмёт её,
# иначе напечатает текст в поле сообщения.
#
# СТАТУС: approved — отправится | draft — нет | needs_input — нужен твой ответ
# УДАЛИТЬ БЛОК = не отвечать на такой вопрос никогда (статус skip).
#
# Вернуть в базу:  venv/bin/python hh_chats.py --import
"""


def fill_answers(db):
    seen = {q for (q,) in db.execute("SELECT qnorm FROM chat_answers")}
    added = 0
    for question, buttons in db.execute(
            "SELECT question, buttons FROM chat_questions"):
        qn = normalize_question(question)
        if not qn or qn in seen:
            continue
        seen.add(qn)
        db.execute("INSERT INTO chat_answers VALUES (?,?,?,?,?,?)",
                   (qn, question.strip(), buttons or "", "", "draft",
                    dt.datetime.now().isoformat(timespec="seconds")))
        added += 1
    db.commit()
    print(f"заведено новых вопросов: {added}, всего в банке чатов: {len(seen)}")


def export_answers(db, path=EDIT_FILE):
    who = {}
    for author, question in db.execute(
            "SELECT author, question FROM chat_questions"):
        who.setdefault(normalize_question(question), set()).add(author or "—")
    comp = {}
    for company, question in db.execute(
            "SELECT company, question FROM chat_questions"):
        comp.setdefault(normalize_question(question), set()).add(company or "—")

    rows = db.execute(
        "SELECT qnorm, question, options, answer, status FROM chat_answers"
    ).fetchall()
    order = {"needs_input": 0, "draft": 1, "approved": 2, "skip": 3}
    rows.sort(key=lambda r: (order.get(r[4], 4), r[1]))

    ids = [qid(r[0]) for r in rows]
    exported = "\n".join("# EXPORTED: " + " ".join(ids[i:i + 8])
                         for i in range(0, len(ids), 8))
    chunks = [CHAT_HEADER + exported + "\n"]
    for qnorm, question, options, answer, status in rows:
        flat = re.sub(r"\s+", " ", question or "").strip()
        chunks.append(
            f"{SEP}\n"
            f"ID: {qid(qnorm)}\n"
            f"СТАТУС: {status}\n"
            f"КОМПАНИЯ: {', '.join(sorted(comp.get(qnorm, []))) or '—'}\n"
            f"СПРАШИВАЕТ: {', '.join(sorted(who.get(qnorm, []))) or '—'}\n"
            f"ВОПРОС: {textwrap.fill(flat, 96, subsequent_indent='        ')}\n"
            f"КНОПКИ: {options or '—'}\n"
            f"ОТВЕТ:\n{(answer or '').strip()}\n")
    chunks.append(SEP + "\n")
    path.write_text("\n".join(chunks), encoding="utf-8")
    print(f"выгружено {len(rows)} вопросов в {path.name}")


def import_answers(db, path=EDIT_FILE):
    if not path.exists():
        raise SystemExit(f"нет файла {path.name} — сначала --export")
    by_id = {qid(q): q for (q,) in db.execute("SELECT qnorm FROM chat_answers")}
    text = path.read_text(encoding="utf-8")

    exported = set()
    for line in text.splitlines():
        if line.startswith("# EXPORTED:"):
            exported.update(line.split(":", 1)[1].split())

    present, updated, bad = set(), 0, []
    for block in re.split(rf"^{SEP}\s*$", text, flags=re.M):
        fields, ans, in_ans = {}, [], False
        for line in block.splitlines():
            if in_ans:
                ans.append(line)
                continue
            if line.strip() == "ОТВЕТ:":
                in_ans = True
                continue
            m = re.match(r"^(ID|СТАТУС):\s*(.*)$", line)
            if m:
                fields[m.group(1)] = m.group(2)
        ident = fields.get("ID", "").strip()
        if not ident:
            continue
        present.add(ident)
        qnorm = by_id.get(ident)
        if not qnorm:
            continue
        status = _parse_status(fields.get("СТАТУС"))
        answer = "\n".join(ans).strip()
        if fields.get("СТАТУС") and status is None:
            bad.append((ident, fields["СТАТУС"].strip()))
            continue
        if status == "approved" and not answer:
            bad.append((ident, "approved с пустым ответом"))
            continue
        db.execute("UPDATE chat_answers SET answer=?, status=?, ts=? WHERE qnorm=?",
                   (answer, status or "draft",
                    dt.datetime.now().isoformat(timespec="seconds"), qnorm))
        updated += 1

    deleted = [by_id[i] for i in sorted(exported - present) if i in by_id]
    for qnorm in deleted:
        db.execute("UPDATE chat_answers SET status='skip' WHERE qnorm=?", (qnorm,))
    db.commit()
    print(f"обновлено: {updated}")
    if deleted:
        print(f"удалено из файла → не отвечаем ({len(deleted)})")
    for ident, why in bad:
        print(f"  не принято {ident}: {why}")


def load_chat_bank(db):
    return {q: a for q, a in db.execute(
        "SELECT qnorm, answer FROM chat_answers "
        "WHERE status='approved' AND TRIM(COALESCE(answer,'')) != ''")}


# ============ отвечающий цикл ============
MAX_TURNS = 6          # сколько реплик подряд отвечаем в одном чате
INPUT_SEL = '[data-qa="text-input"]'


def chat_state(page):
    """Сообщения с пометкой, чьи они. Свои узнаём по индикатору прочтения."""
    return page.evaluate("""() => {
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
            out.push({
                id: qa.replace('chatik-chat-message-',''),
                author,
                own: !!el.querySelector('[data-qa="chat-bubble-indicators"]'),
                text,
            });
        }
        return out;
    }""")


def pending_question(msgs):
    """Последний вопрос собеседника, на который мы ещё не ответили."""
    for m in reversed(msgs):
        if m["own"]:
            return None          # последнее слово наше — отвечать нечего
        if is_question(m["text"]) and m["author"] not in SKIP_AUTHORS:
            return m
    return None


def send_reply(page, text, dry):
    """Нажать кнопку с таким текстом или напечатать ответ и отправить."""
    exact = [b for b in page.evaluate(
        """() => [...document.querySelectorAll('button')]
             .filter(b => !b.getAttribute('data-qa'))
             .map(b => (b.textContent||'').trim())""")
        if b and b.strip().lower() == text.strip().lower()]
    if exact:
        if dry:
            print(f"      [сухой] нажал бы кнопку {text!r}")
            return True
        page.get_by_role("button", name=text, exact=True).first.click()
        return True

    field = page.locator(INPUT_SEL)
    if field.count() == 0 or not field.first.is_visible():
        print("      поля ввода нет, пропускаю")
        return False
    if dry:
        print(f"      [сухой] напечатал бы: {text[:110]!r}")
        return True
    field.first.click()
    field.first.fill(text)
    hh.pause(1, 2)
    page.keyboard.press("Enter")
    return True


def reply(limit=None, dry=True):
    db = init_db()
    bank = load_chat_bank(db)
    print(f"одобренных ответов в банке: {len(bank)}"
          + ("  [СУХОЙ ПРОГОН]" if dry else "  [БОЕВОЙ РЕЖИМ]"))
    if not bank:
        raise SystemExit("банк пуст: заполни chat_answers и одобри ответы")

    ws = hh.open_profile()
    sent = 0
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(ws)
        ctx = browser.contexts[0]
        page = ctx.new_page()
        page.set_default_navigation_timeout(90000)
        try:
            chats = list_chats(page)
            if limit:
                chats = chats[:limit]
            print(f"чатов к обходу: {len(chats)}\n")
            for c in chats:
                page.goto(f"https://hh.ru/chat/{c['id']}", wait_until="commit")
                page.wait_for_timeout(5000)
                for turn in range(MAX_TURNS):
                    q = pending_question(chat_state(page))
                    if not q:
                        break
                    answer = bank.get(normalize_question(q["text"]))
                    if not answer:
                        if turn == 0:
                            print(f"  [{c['company'][:26]}] нет одобренного ответа: "
                                  f"{re.sub(r'  +', ' ', q['text'])[:80]}")
                        break
                    print(f"  [{c['company'][:26]}] отвечаю на: "
                          f"{re.sub(r'  +', ' ', q['text'])[:70]}")
                    if not send_reply(page, answer, dry):
                        break
                    sent += 1
                    if dry:
                        break          # в сухом режиме диалог не двигается
                    hh.pause(4, 8)
                    page.wait_for_timeout(3000)
                hh.pause(2, 4)
        finally:
            page.close()
            hh.close_profile()
    print(f"\n{'было бы отправлено' if dry else 'отправлено'}: {sent}")

if __name__ == "__main__":
    main()
