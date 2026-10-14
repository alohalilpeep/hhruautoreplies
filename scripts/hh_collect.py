"""Сбор вакансий и компаний из поиска hh.ru — без откликов.

    python scripts/hh_collect.py hh3 .env.search2

Ссылка поиска берётся из SEARCH_URL указанного файла в папке аккаунта, число страниц —
из COLLECT_PAGES (по умолчанию 40, у hh больше не бывает). Карточки выдачи пишутся
в таблицу collected_vacancies базы аккаунта: вакансия, компания, ссылка на работодателя,
зарплата, опыт, адрес. На сами вакансии не заходим — отбор по тексту вакансии делает
запрос hh (DESCRIPTION:…), так меньше нагрузки и капчи.
"""
import os
import re
import sys
from pathlib import Path

ACCS = {"hh": "hh_autoreply", "hh2": "hh_autoreply_2", "hh3": "hh_autoreply_3"}
BASE = Path("/Users/mr_macbook/Documents")


def read_env(path):
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Z_]+)=(.*)$", line.strip())
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def main():
    if len(sys.argv) < 3 or sys.argv[1] not in ACCS:
        raise SystemExit(__doc__)
    acc = BASE / ACCS[sys.argv[1]]
    conf = read_env(acc / sys.argv[2])
    url = conf.get("SEARCH_URL", "")
    if not url:
        raise SystemExit(f"в {sys.argv[2]} пустой SEARCH_URL")
    pages = int(conf.get("COLLECT_PAGES") or 40)
    query = sys.argv[2]

    os.chdir(acc)
    sys.path.insert(0, str(acc))
    import hh_autoapply as hh                      # .env аккаунта: профиль, имя
    import hh_searchapply as sa
    from playwright.sync_api import sync_playwright

    db = hh.init_db()
    db.execute("""CREATE TABLE IF NOT EXISTS collected_vacancies (
        id TEXT PRIMARY KEY, query TEXT, url TEXT, title TEXT, company TEXT,
        employer_url TEXT, salary TEXT, experience TEXT, address TEXT, work_format TEXT,
        first_seen TEXT, last_seen TEXT)""")
    db.commit()
    known = {r[0] for r in db.execute("SELECT id FROM collected_vacancies")}

    sep = "&" if "?" in url else "?"
    seen, new, stop = set(), 0, ""
    ws = hh.open_profile()
    try:
        with sync_playwright() as p:
            ctx = p.chromium.connect_over_cdp(ws).contexts[0]
            page = hh.fresh_page(ctx)
            page.set_default_navigation_timeout(hh.NAV_TIMEOUT)
            for n in range(pages):
                page.goto(f"{url}{sep}page={n}", wait_until="domcontentloaded")
                hh.pause(3, 6)
                sa.mark_tab(page, f"{hh.ACCOUNT_NAME} сбор")
                if hh.captcha_present(page) and not hh.wait_captcha(page):
                    stop = "капча"
                    break
                cards = sa.cards_on_page(page)
                total = cards.count()
                if not total:
                    stop = f"страница {n} пустая"
                    break
                now = hh.dt.datetime.now().isoformat(timespec="seconds")
                page_new = 0
                for i in range(total):
                    card = cards.nth(i)
                    try:
                        vid, info = sa.card_info(card)
                        emp = card.locator('[data-qa="vacancy-serp__vacancy-employer"]').first
                        href = emp.get_attribute("href") if emp.count() else ""
                    except Exception:
                        continue
                    if not vid or vid in seen:
                        continue
                    seen.add(vid)
                    if vid not in known:
                        new += 1
                        page_new += 1
                    db.execute(
                        "INSERT INTO collected_vacancies VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(id) DO UPDATE SET last_seen = excluded.last_seen, "
                        " salary = excluded.salary, title = excluded.title",
                        (vid, query, f"https://hh.ru/vacancy/{vid}", info.get("title"),
                         info.get("company"),
                         ("https://hh.ru" + href.split("?")[0]) if href and href.startswith("/")
                         else (href or "").split("?")[0],
                         info.get("salary"), info.get("experience"), info.get("address"),
                         info.get("work_format"), now, now))
                db.commit()
                print(f"страница {n}: карточек {total}, новых {page_new}", flush=True)
                if total < 20:                     # последняя страница выдачи
                    stop = "конец выдачи"
                    break
    finally:
        hh.close_profile()

    companies = db.execute(
        "SELECT company, COUNT(*) FROM collected_vacancies WHERE query = ? "
        "GROUP BY company ORDER BY COUNT(*) DESC", (query,)).fetchall()
    print(f"\nостановка: {stop or f'прошёл {pages} страниц'}")
    print(f"вакансий в этом проходе: {len(seen)}, новых: {new}")
    print(f"всего по запросу: {sum(c for _, c in companies)} вакансий, {len(companies)} компаний")
    for name, cnt in companies[:15]:
        print(f"  {cnt:3d}  {name}")


if __name__ == "__main__":
    main()
