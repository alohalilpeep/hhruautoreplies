"""Отклики на getmatch.ru из профиля BitBrowser аккаунта.

    python scripts/getmatch_apply.py <hh|hh2|hh3> [--dry] [--limit N]

--dry    только показать, на что откликнулись бы
--limit  сколько откликов максимум за этот запуск (по умолчанию DAILY)
--export вакансии с вопросами работодателя в письме — частью 5 в конец
         свежего бланка (после questions.sh export и ai_chats.sh export)
--take   забрать ответы части 5 и отправить эти отклики с твоим текстом

Как устроено. Вакансии берём из API getmatch (/api/offers) из-под входа
профиля: специальность DevOps и SRE, грейд Middle и Senior, только
удалённые вакансии. В специальность getmatch кладёт и чужие должности
(«Начальник отдела», «Product Owner»), поэтому дальше фильтр по названию и
стоп-лист аккаунта (blacklist.txt). Отклик — форма вакансии: «Откликнуться»,
письмо из letter.txt аккаунта, «Отправить отклик». Зарплату getmatch
подставляет из профиля сам. Что отклик дошёл, проверяем по API: у вакансии
появляется поле application. Всё пишется в таблицу gm_responses базы аккаунта.

Профиль не закрываем (лимит открытий BitBrowser), в конце чистим вкладки.
"""
import datetime as dt
import json
import os
import re
import sys

BASE = "/Users/mr_macbook/Documents"
ACCS = {"hh": "hh_autoreply", "hh2": "hh_autoreply_2", "hh3": "hh_autoreply_3"}
# Для сбора — весь DevOps и SRE, любой формат и грейд: это пара запросов,
# а в базе видно рынок целиком. Откликаемся только по QUERY.
COLLECT = ("/api/offers?sa=any&pa=all&sp=dev_ops&sp=sre"
           "&limit=50&offset={offset}&p={page}")
# только удалёнка: l=remote. Москву (офис и гибрид) убрали 13.10.2026.
QUERY = ("/api/offers?sa=any&pa=all&sp=dev_ops&sp=sre&se=middle&se=senior"
         "&l=remote&limit=50&offset={offset}&p={page}")
TITLE_RE = re.compile(
    r"devops|dev ops|\bsre\b|site reliab|platform|kubernetes|k8s|инфраструктур"
    r"|внедрени|linux|cloud|облак|mlops|observability|мониторинг", re.I)
DAILY = 30


def account(short):
    path = f"{BASE}/{ACCS[short]}"
    os.chdir(path)
    sys.path.insert(0, path)
    import hh_autoapply as hh
    return hh


def init_table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS gm_responses (
        id INTEGER PRIMARY KEY, url TEXT, position TEXT, company TEXT,
        status TEXT, letter_sent INTEGER, ts TEXT)""")
    # всё, что видели в выдаче getmatch, — не только то, куда откликнулись
    db.execute("""CREATE TABLE IF NOT EXISTS gm_vacancies (
        id INTEGER PRIMARY KEY, url TEXT, position TEXT,
        company_key TEXT, company TEXT, published_at TEXT,
        salary_from INTEGER, salary_to INTEGER, salary_currency TEXT,
        salary_taxes TEXT, salary_hidden INTEGER, formats TEXT, cities TEXT,
        skills TEXT, english TEXT, cover_letter_required INTEGER,
        applied INTEGER, first_seen TEXT, last_seen TEXT)""")
    # вакансии, где работодатель задал вопросы в письме: ждут ответа в бланке
    db.execute("""CREATE TABLE IF NOT EXISTS gm_pending (
        id INTEGER PRIMARY KEY, url TEXT, position TEXT, company TEXT,
        questions TEXT, answer TEXT, status TEXT, ts TEXT)""")
    # ключ компании — её ссылка на getmatch (id API не отдаёт), у скрытых — название
    db.execute("""CREATE TABLE IF NOT EXISTS gm_companies (
        key TEXT PRIMARY KEY, name TEXT, url TEXT,
        first_seen TEXT, last_seen TEXT, vacancies INTEGER)""")
    db.commit()


def as_text(v):
    """Поле API в строку: у части полей вместо строки словарь с подписью."""
    if isinstance(v, dict):
        return v.get("name") or v.get("title") or v.get("value") or json.dumps(v, ensure_ascii=False)
    return v


def remember_offers(db, offers):
    """Записать вакансии и компании из выдачи. Повторный запуск обновляет
    last_seen и меняющиеся поля, first_seen не трогает."""
    ts = dt.datetime.now().isoformat(timespec="seconds")
    for o in offers:
        comp = o.get("company") or {}
        key = (comp.get("url") or "").rsplit("/", 1)[-1] or (comp.get("name") or "").strip() or None
        locs = o.get("location_requirements") or []
        formats = ",".join(sorted({l.get("format") for l in locs if l.get("format")}))
        cities = ",".join(sorted({l.get("city") for l in locs if l.get("city")}))
        skills = ",".join(s.get("name", "") for s in (o.get("skills_objects") or [])
                          if isinstance(s, dict))
        row = (o["id"], "https://getmatch.ru" + o["url"], o.get("position"),
               key, (comp.get("name") or "").strip(), o.get("published_at"),
               o.get("salary_display_from"), o.get("salary_display_to"),
               o.get("salary_currency"), o.get("salary_taxes"),
               1 if o.get("salary_hidden") else 0, formats, cities, skills,
               as_text(o.get("english_level")), 1 if o.get("cover_letter_required") else 0,
               1 if o.get("application") else 0)
        db.execute("""INSERT INTO gm_vacancies VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET position=excluded.position, company=excluded.company,
              salary_from=excluded.salary_from, salary_to=excluded.salary_to,
              salary_currency=excluded.salary_currency, salary_taxes=excluded.salary_taxes,
              salary_hidden=excluded.salary_hidden, formats=excluded.formats,
              cities=excluded.cities, skills=excluded.skills, english=excluded.english,
              cover_letter_required=excluded.cover_letter_required,
              applied=MAX(gm_vacancies.applied, excluded.applied), last_seen=excluded.last_seen""",
                   row + (ts, ts))
        if key:
            url = "https://getmatch.ru" + comp["url"] if comp.get("url") else None
            db.execute("""INSERT INTO gm_companies VALUES (?,?,?,?,?,0)
                ON CONFLICT(key) DO UPDATE SET name=excluded.name, url=excluded.url,
                  last_seen=excluded.last_seen""",
                       (key, (comp.get("name") or "").strip(), url, ts, ts))
    db.execute("""UPDATE gm_companies SET vacancies =
        (SELECT COUNT(*) FROM gm_vacancies v WHERE v.company_key = gm_companies.key)""")
    db.commit()


def sent_today(db):
    today = dt.date.today().isoformat()
    return db.execute("SELECT COUNT(*) FROM gm_responses WHERE status='applied' AND ts >= ?",
                      (today,)).fetchone()[0]


def fetch_offers(page, query=None):
    """Все вакансии по запросу, страница за страницей."""
    query = query or QUERY
    out, offset, n = [], 0, 1
    while True:
        d = page.evaluate("u => fetch(u, {credentials: 'include'}).then(r => r.json())",
                          query.format(offset=offset, page=n))
        batch = d.get("offers") or []
        out += batch
        offset += len(batch)
        n += 1
        if not batch or offset >= d.get("meta", {}).get("total", 0):
            return out


def has_questions(offer):
    """Письмо обязательно или в подсказке к нему вопросы работодателя."""
    ph = offer.get("cover_letter_placeholder") or ""
    return bool(offer.get("cover_letter_required")) or "?" in ph


def offer_state(page, offer_id):
    """Есть ли у вакансии отклик — спрашиваем API заново."""
    d = page.evaluate("u => fetch(u, {credentials: 'include'}).then(r => r.json())",
                      f"/api/offers/{offer_id}")
    return (d or {}).get("application")


def apply_one(hh, page, offer, letter):
    """Открыть вакансию, заполнить письмо, отправить. Возвращает статус."""
    page.goto("https://getmatch.ru" + offer["url"], wait_until="domcontentloaded")
    hh.pause(3, 5)
    btn = page.get_by_role("button", name=re.compile(r"^\s*Откликнуться\s*$")).first
    if not btn.count():
        return "no_button", False
    btn.click(timeout=10000)
    hh.pause(2, 3)
    area = page.locator("textarea[name=cover_letter]")
    letter_sent = False
    if area.count() and area.first.is_visible():
        area.first.fill(letter)
        letter_sent = True
        hh.pause(1, 2)
    send = page.get_by_role("button", name=re.compile(r"Отправить отклик")).first
    if not send.count():
        return "no_form", letter_sent
    send.click(timeout=10000)
    hh.pause(3, 5)
    # что форма не ругается на пустые обязательные поля
    errors = page.locator("[class*=error]:visible, [class*=invalid]:visible")
    if errors.count():
        return "form_error:" + re.sub(r"\s+", " ", errors.first.inner_text())[:80], letter_sent
    return "sent", letter_sent


# ---------------- бланк: часть 5 ----------------
PART5 = "# ЧАСТЬ 5. GETMATCH: ВОПРОСЫ РАБОТОДАТЕЛЯ В ПИСЬМЕ"
BAR = "# " + "=" * 70
HEAD5 = f"""
{BAR}
{PART5}
#
# Работодатель задал вопросы прямо в сопроводительном письме. Напиши после
# «ОТВЕТ:» письмо с ответами — оно уйдёт целиком вместо обычного письма.
# Строку GETMATCH не трогай. Не хочешь откликаться — СТАТУС: skip.
{BAR}
"""


def latest_blank(short):
    from pathlib import Path
    files = sorted(Path(f"{BASE}/{ACCS[short]}/questions/new").glob("*.txt"),
                   key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def export_blank(short):
    """Дописать часть 5 в конец свежего бланка (после частей 3–4 ботов)."""
    hh = account(short)
    db = hh.init_db()
    init_table(db)
    rows = db.execute("SELECT id, url, position, company, questions FROM gm_pending "
                      "WHERE status='waiting' ORDER BY ts").fetchall()
    path = latest_blank(short)
    if path is None:
        print(f"getmatch {short}: бланка нет — сначала questions.sh export")
        return
    text = path.read_text(encoding="utf-8")
    i = text.find(PART5)
    if i >= 0:                                   # заменить старую часть 5
        text = text[:text.rfind(BAR, 0, i)].rstrip() + "\n"
    if rows:
        blocks = []
        for vid, url, pos, comp, qs in rows:
            asked = "\n".join("# " + l for l in (qs or "").splitlines())
            blocks.append(f"---\nGETMATCH: {vid}\nСТАТУС: needs_input\nКОМПАНИЯ: {comp}\n"
                          f"ВАКАНСИЯ: {pos}\nССЫЛКА: {url}\n# ВОПРОСЫ РАБОТОДАТЕЛЯ:\n"
                          f"{asked}\nОТВЕТ:\n\n")
        text = text.rstrip() + "\n" + HEAD5 + "\n" + "".join(blocks) + "---\n"
    path.write_text(text, encoding="utf-8")
    print(f"getmatch {short}: в {path.name} вакансий с вопросами в письме: {len(rows)}")
    for vid, url, pos, comp, _ in rows:
        print(f"  ⚑ {comp[:30]} | {pos[:60]}")


def take_blank(short):
    """Забрать ответы части 5: approved — в gm_pending.answer, skip — skip."""
    hh = account(short)
    from hh_answers import _parse_status
    db = hh.init_db()
    init_table(db)
    path = latest_blank(short)
    got = skipped = 0
    if path is not None:
        for block in re.split(r"(?m)^---\s*$", path.read_text(encoding="utf-8")):
            m = re.search(r"(?m)^GETMATCH:\s*(\d+)", block)
            if not m:
                continue
            vid = int(m.group(1))
            st = _parse_status((re.search(r"(?m)^СТАТУС:\s*(.*)$", block) or [None, ""])[1])
            ans = block.split("\nОТВЕТ:", 1)[1] if "\nОТВЕТ:" in block else ""
            ans = "\n".join(l for l in ans.splitlines() if not l.lstrip().startswith("#")).strip()
            if st == "skip":
                db.execute("UPDATE gm_pending SET status='skip' WHERE id=?", (vid,))
                skipped += 1
            # Написанный ответ уже и есть согласие: в шапке части 5 про статус
            # approved ничего не сказано, и ответ с needs_input не терялся бы зря.
            elif ans:
                db.execute("UPDATE gm_pending SET answer=?, status='answered' WHERE id=?",
                           (ans, vid))
                got += 1
        db.commit()
    print(f"getmatch {short}: из бланка принято ответов {got}, skip {skipped}")
    return got


def main(short, dry, limit, only_answered=False):
    hh = account(short)
    from playwright.sync_api import sync_playwright
    db = hh.init_db()
    init_table(db)
    done = {r[0] for r in db.execute("SELECT id FROM gm_responses WHERE status IN "
                                     "('applied', 'skipped')")}
    left = min(limit, DAILY - sent_today(db))
    ws = hh.open_profile()
    try:
        with sync_playwright() as p:
            ctx = p.chromium.connect_over_cdp(ws).contexts[0]
            page = hh.fresh_page(ctx)
            page.goto("https://getmatch.ru/vacancies", wait_until="domcontentloaded")
            hh.pause(2, 3)
            if "Войти" in page.inner_text("body")[:3000]:
                print("Профиль не залогинен на getmatch.ru — войди руками и перезапусти")
                return
            remember_offers(db, fetch_offers(page, COLLECT))
            offers = fetch_offers(page)
            remember_offers(db, offers)
            print(f"в базе вакансий getmatch: "
                  f"{db.execute('SELECT COUNT(*) FROM gm_vacancies').fetchone()[0]}, "
                  f"компаний: {db.execute('SELECT COUNT(*) FROM gm_companies').fetchone()[0]}")
            todo, questions = [], []
            skipped = {"уже откликался": 0, "не та должность": 0, "стоп-лист": 0}
            for o in offers:
                comp = (o.get("company") or {}).get("name", "")
                if o.get("application") or o["id"] in done:
                    skipped["уже откликался"] += 1
                elif not TITLE_RE.search(o["position"]):
                    skipped["не та должность"] += 1
                elif hh.is_blacklisted(comp):
                    skipped["стоп-лист"] += 1
                elif has_questions(o):
                    # Работодатель ждёт в письме ответов на свои вопросы. Обычное
                    # письмо там — отписка: 13.10 так ушёл отклик в Островок
                    # без ответов на шесть вопросов. Сами такие не отправляем.
                    skipped.setdefault("вопросы в письме", 0)
                    skipped["вопросы в письме"] += 1
                    questions.append(o)
                else:
                    todo.append(o)
            print(f"getmatch {short}: вакансий {len(offers)}, к отклику {len(todo)}, "
                  f"пропущено {skipped}; лимит на запуск {left}")
            ts = dt.datetime.now().isoformat(timespec="seconds")
            for o in questions:
                print(f"  ⚑ вопросы в письме: {o['position'][:50]} | "
                      f"{o['company'].get('name', '')[:25]} | {o['url']}")
                db.execute("""INSERT INTO gm_pending VALUES (?,?,?,?,?,NULL,'waiting',?)
                    ON CONFLICT(id) DO UPDATE SET questions=excluded.questions""",
                           (o["id"], "https://getmatch.ru" + o["url"], o["position"],
                            o["company"].get("name", ""),
                            (o.get("cover_letter_placeholder") or "").strip(), ts))
            db.commit()
            # Ответы, которые человек дал в бланке: отправляем первыми, письмом
            # идёт его текст. Остальное — как обычно, обычным письмом.
            answered = {r[0]: r[1] for r in db.execute(
                "SELECT id, answer FROM gm_pending WHERE status='answered'")}
            by_id = {o["id"]: o for o in offers}
            ready = [by_id[i] for i in answered if i in by_id
                     and not by_id[i].get("application") and i not in done]
            todo = ready + ([] if only_answered else todo)
            if dry:
                if todo:                     # проверка, что API карточки отвечает
                    st = page.evaluate("u => fetch(u, {credentials: 'include'})"
                                       ".then(r => r.status)", f"/api/offers/{todo[0]['id']}")
                    print(f"  /api/offers/<id> отвечает: HTTP {st}")
                for o in todo:
                    print(f"  [сухой] {o['published_at'][:10]} {o['position'][:55]} | "
                          f"{o['company'].get('name', '')[:25]}")
                return
            sent = 0
            for o in todo:
                if sent >= left:
                    print("лимит достигнут, останавливаюсь")
                    break
                comp = o["company"].get("name", "")
                letter = answered.get(o["id"]) or hh.render_letter(hh.clean_title(o["position"]), comp)
                try:
                    status, with_letter = apply_one(hh, page, o, letter)
                except Exception as e:
                    status, with_letter = f"error:{type(e).__name__}", False
                if status == "sent":
                    status = "applied" if offer_state(page, o["id"]) else "unknown"
                ts = dt.datetime.now().isoformat(timespec="seconds")
                db.execute("INSERT OR REPLACE INTO gm_responses VALUES (?,?,?,?,?,?,?)",
                           (o["id"], "https://getmatch.ru" + o["url"], o["position"], comp,
                            status, 1 if with_letter else 0, ts))
                db.commit()
                print(f"[{status}{' +письмо' if with_letter else ''}] {o['position'][:55]} | {comp}")
                if status == "applied":
                    sent += 1
                    db.execute("UPDATE gm_vacancies SET applied=1 WHERE id=?", (o["id"],))
                    db.execute("UPDATE gm_pending SET status='sent', ts=? WHERE id=?",
                               (ts, o["id"]))
                    db.commit()
                hh.pause(20, 45)
            print(f"getmatch {short}: отправлено {sent}, за сегодня {sent_today(db)}")
            page.close()
    finally:
        hh.close_profile()


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a or a[0] not in ACCS:
        print(__doc__)
        sys.exit(2)
    if "--export" in a:
        export_blank(a[0])
    elif "--take" in a:
        if take_blank(a[0]):                     # отправить только отвеченные
            main(a[0], False, DAILY, only_answered=True)
    else:
        lim = int(a[a.index("--limit") + 1]) if "--limit" in a else DAILY
        main(a[0], "--dry" in a, lim)
