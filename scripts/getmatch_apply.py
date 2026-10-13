"""Отклики на getmatch.ru из профиля BitBrowser аккаунта.

    python scripts/getmatch_apply.py <hh|hh2|hh3> [--dry] [--limit N]

--dry   только показать, на что откликнулись бы
--limit сколько откликов максимум за этот запуск (по умолчанию DAILY)

Как устроено. Вакансии берём из API getmatch (/api/offers) из-под входа
профиля: специальность DevOps и SRE, грейд Middle и Senior, удалёнка или
Москва — как поиск на hh. В специальность getmatch кладёт и чужие должности
(«Начальник отдела», «Product Owner»), поэтому дальше фильтр по названию и
стоп-лист аккаунта (blacklist.txt). Отклик — форма вакансии: «Откликнуться»,
письмо из letter.txt аккаунта, «Отправить отклик». Зарплату getmatch
подставляет из профиля сам. Что отклик дошёл, проверяем по API: у вакансии
появляется поле application. Всё пишется в таблицу gm_responses базы аккаунта.

Профиль не закрываем (лимит открытий BitBrowser), в конце чистим вкладки.
"""
import datetime as dt
import os
import re
import sys

BASE = "/Users/mr_macbook/Documents"
ACCS = {"hh": "hh_autoreply", "hh2": "hh_autoreply_2", "hh3": "hh_autoreply_3"}
QUERY = ("/api/offers?sa=any&pa=all&sp=dev_ops&sp=sre&se=middle&se=senior"
         "&l=remote&l=moscow&limit=50&offset={offset}&p={page}")
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
    db.commit()


def sent_today(db):
    today = dt.date.today().isoformat()
    return db.execute("SELECT COUNT(*) FROM gm_responses WHERE status='applied' AND ts >= ?",
                      (today,)).fetchone()[0]


def fetch_offers(page):
    """Все вакансии по запросу, страница за страницей."""
    out, offset, n = [], 0, 1
    while True:
        d = page.evaluate("u => fetch(u, {credentials: 'include'}).then(r => r.json())",
                          QUERY.format(offset=offset, page=n))
        batch = d.get("offers") or []
        out += batch
        offset += len(batch)
        n += 1
        if not batch or offset >= d.get("meta", {}).get("total", 0):
            return out


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


def main(short, dry, limit):
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
            offers = fetch_offers(page)
            todo, skipped = [], {"уже откликался": 0, "не та должность": 0, "стоп-лист": 0}
            for o in offers:
                comp = (o.get("company") or {}).get("name", "")
                if o.get("application") or o["id"] in done:
                    skipped["уже откликался"] += 1
                elif not TITLE_RE.search(o["position"]):
                    skipped["не та должность"] += 1
                elif hh.is_blacklisted(comp):
                    skipped["стоп-лист"] += 1
                else:
                    todo.append(o)
            print(f"getmatch {short}: вакансий {len(offers)}, к отклику {len(todo)}, "
                  f"пропущено {skipped}; лимит на запуск {left}")
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
                letter = hh.render_letter(hh.clean_title(o["position"]), comp)
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
    lim = int(a[a.index("--limit") + 1]) if "--limit" in a else DAILY
    main(a[0], "--dry" in a, lim)
