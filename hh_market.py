"""
Анализ рынка: что за технологии просят и кто именно.

    python hh_market.py --collect        обойти выдачу и снять факты
    python hh_market.py --collect 40     ограничить числом вакансий
    python hh_market.py --rescrape 250   пересобрать навыки новым правилом
    python hh_market.py --refresh 14     перепроверить давно не виденные
    python hh_market.py --report         рейтинг навыков
    python hh_market.py --merge ../hh_autoreply_2/hh_responses.db

Сбор ничего не нажимает и не отправляет: открывает страницу вакансии и
читает. Для hh это обычный просмотр, поэтому режим безопасен и на
аккаунте, который недавно ловил капчу.

Факты о вакансии от аккаунта не зависят, а баз у нас несколько — поэтому
есть --merge: подтянуть собранное другим аккаунтом в текущую базу.
"""
import sqlite3
import sys

from playwright.sync_api import sync_playwright

import hh_autoapply as hh

# Названия, которые тащит семантическое расширение поиска hh. Считать по ним
# спрос бессмысленно: это соседний рынок, а не наш.
OFF_PROFILE = (
    "1с", "битрикс", "поддержк", "хелпдеск", "helpdesk", "разработчик",
    "developer", "аналитик", "тестировщик", "qa", "data engineer",
    "data scientist", "ml-", "стажёр", "стажер",
)


def collect(limit=None):
    db = hh.init_db()
    ws = hh.open_profile()
    seen = {r[0] for r in db.execute("SELECT vacancy_id FROM vacancy_facts")}
    new = done = 0
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(ws)
        page = browser.contexts[0].new_page()
        page.set_default_navigation_timeout(hh.NAV_TIMEOUT)
        try:
            url = hh.MARKET_URL or hh.SEARCH_URL
            if hh.MARKET_URL:
                print(f"широкий рыночный запрос, до {hh.MARKET_PAGES} страниц")
            vacancies = hh.collect(page, url, hh.MARKET_PAGES)
            todo = [(v, u) for v, u in vacancies.items() if v not in seen]
            print(f"в выдаче {len(vacancies)}, без фактов {len(todo)}")
            if limit:
                todo = todo[:limit]
            for vid, url in todo:
                try:
                    page.goto(url, wait_until="domcontentloaded")
                    hh.pause(1.5, 3)
                    if hh.captcha_present(page):
                        print("\nhh показал капчу — останавливаюсь.")
                        break
                    facts = hh.scrape_facts(page)
                    hh.save_facts(db, vid, facts)
                    done += 1
                    new += bool(facts and facts.get("skills"))
                    title = hh.text_or(page, hh.SEL["title"])[:44]
                    n = len(facts.get("skills") or []) if facts else 0
                    print(f"  {vid} навыков {n:2}  {title}")
                except Exception as e:
                    print(f"  {vid}: {type(e).__name__}")
                    if "closed" in str(e).lower():
                        break
        finally:
            page.close()
    print(f"\nснято фактов: {done}, из них с навыками: {new}")


def rescrape(limit=None):
    """Пересобрать навыки по вакансиям, которые уже в базе.

    Прежние строки помечаются stale, а не удаляются: если новое правило
    вернёт пусто, надо видеть, что было раньше. Отчёт считает только ok.
    """
    db = hh.init_db()
    todo = [r[0] for r in db.execute(
        "SELECT vacancy_id FROM vacancy_facts ORDER BY scraped_ts")]
    already = {r[0] for r in db.execute(
        "SELECT DISTINCT vacancy_id FROM vacancy_skills WHERE status='stale'")}
    if "--all" not in sys.argv:
        todo = [v for v in todo if v not in already]  # продолжаем с места остановки
    print(f"вакансий в базе: {len(todo)} к пересбору")
    if limit:
        todo = todo[:limit]
    ws = hh.open_profile()
    done = with_skills = 0
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(ws)
        page = browser.contexts[0].new_page()
        page.set_default_navigation_timeout(hh.NAV_TIMEOUT)
        try:
            for vid in todo:
                try:
                    page.goto(f"https://hh.ru/vacancy/{vid}",
                              wait_until="domcontentloaded")
                    hh.pause(1.5, 3)
                    if hh.captcha_present(page):
                        print("\nhh показал капчу — останавливаюсь.")
                        break
                    facts = hh.scrape_facts(page)
                    # старое в архив до вставки нового
                    db.execute("UPDATE vacancy_skills SET status='stale' "
                               "WHERE vacancy_id=?", (vid,))
                    hh.save_facts(db, vid, facts)
                    n = len(facts.get("skills") or []) if facts else 0
                    done += 1
                    with_skills += bool(n)
                    if done % 25 == 0:
                        print(f"  ...{done}, с навыками {with_skills}")
                except Exception as e:
                    print(f"  {vid}: {type(e).__name__}")
                    if "closed" in str(e).lower():
                        break
        finally:
            page.close()
    print(f"\nпересобрано: {done}, из них с навыками: {with_skills}")


def refresh(days=14, limit=None):
    """Перепроверить вакансии, которых давно не видели.

    Без этого выборка застывает на дне сбора: закрытые вакансии копятся,
    а рейтинг «что востребовано сейчас» становится архивным снимком.
    """
    db = hh.init_db()
    todo = [r[0] for r in db.execute(
        "SELECT vacancy_id FROM vacancy_facts "
        "WHERE scraped_ts < datetime('now', ?) ORDER BY scraped_ts",
        (f"-{days} day",))]
    print(f"не проверялись больше {days} дней: {len(todo)}")
    if limit:
        todo = todo[:limit]
    if not todo:
        return
    ws = hh.open_profile()
    done = gone = 0
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(ws)
        page = browser.contexts[0].new_page()
        page.set_default_navigation_timeout(hh.NAV_TIMEOUT)
        try:
            for vid in todo:
                try:
                    page.goto(f"https://hh.ru/vacancy/{vid}",
                              wait_until="domcontentloaded")
                    hh.pause(1.5, 3)
                    if hh.captcha_present(page):
                        print("\nhh показал капчу — останавливаюсь.")
                        break
                    facts = hh.scrape_facts(page)
                    db.execute("UPDATE vacancy_skills SET status='stale' "
                               "WHERE vacancy_id=?", (vid,))
                    hh.save_facts(db, vid, facts)
                    done += 1
                    gone += bool(facts and facts.get("archived"))
                    if done % 25 == 0:
                        print(f"  ...{done}, ушло в архив {gone}")
                except Exception as e:
                    print(f"  {vid}: {type(e).__name__}")
                    if "closed" in str(e).lower():
                        break
        finally:
            page.close()
    print(f"\nперепроверено: {done}, из них закрылось: {gone}")


def merge(other_path):
    """Подтянуть факты из базы другого аккаунта. Ключ — id вакансии,
    поэтому повтор безвреден."""
    db = hh.init_db()
    db.execute("ATTACH DATABASE ? AS src", (other_path,))
    try:
        f = db.execute("INSERT OR IGNORE INTO vacancy_facts "
                       "SELECT * FROM src.vacancy_facts").rowcount
        s = db.execute("INSERT OR IGNORE INTO vacancy_skills "
                       "SELECT * FROM src.vacancy_skills").rowcount
        db.commit()
        print(f"добавлено фактов: {f}, навыков: {s}")
    except sqlite3.OperationalError as e:
        print(f"не вышло: {e}")
    finally:
        db.execute("DETACH DATABASE src")


def off_profile(title):
    low = (title or "").lower()
    return any(w in low for w in OFF_PROFILE)


def report(db=None):
    db = db or hh.init_db()
    rows = db.execute("""
        SELECT s.skill, s.vacancy_id, COALESCE(r.company,''), COALESCE(r.title,'')
        FROM vacancy_skills s LEFT JOIN responses r ON r.id = s.vacancy_id
        LEFT JOIN vacancy_facts f ON f.vacancy_id = s.vacancy_id
        WHERE COALESCE(s.status,'ok') = 'ok'
          AND COALESCE(f.status,'active') = 'active'""").fetchall()
    kept, dropped = {}, set()
    for skill, vid, company, title in rows:
        if off_profile(title):
            dropped.add(vid)
            continue
        kept.setdefault(skill, set()).add((vid, company))
    vac = {v for pairs in kept.values() for v, _ in pairs}
    print(f"вакансий в выборке: {len(vac)}, отброшено как не наш профиль: {len(dropped)}")
    print(f"уникальных навыков: {len(kept)}\n")
    # Считаем по компаниям, а не по вакансиям. Одна кадровая контора
    # размещает сорок одинаковых вакансий, и по числу вакансий «Ремонт ПК»
    # обгоняет Kubernetes. Спрос — это сколько РАЗНЫХ работодателей ищут.
    rank = []
    for skill, pairs in kept.items():
        comps = {c for _, c in pairs if c}
        if comps:
            rank.append((skill, len(comps), len(pairs)))
    rank.sort(key=lambda x: (-x[1], x[0]))

    print(f"{'навык':32} {'компаний':>9} {'вакансий':>9}  пометка")
    print("-" * 68)
    for skill, nc, nv in rank[:25]:
        # много вакансий на мало компаний — массовый постинг, а не спрос
        mark = "← дубли одной компании" if nv / nc >= 4 else ""
        print(f"{skill[:32]:32} {nc:9} {nv:9}  {mark}")


def main():
    args = sys.argv[1:]
    if "--collect" in args:
        i = args.index("--collect")
        lim = int(args[i + 1]) if len(args) > i + 1 and args[i + 1].isdigit() else None
        collect(lim)
    elif "--rescrape" in args:
        i = args.index("--rescrape")
        lim = int(args[i + 1]) if len(args) > i + 1 and args[i + 1].isdigit() else None
        rescrape(lim)
    elif "--refresh" in args:
        i = args.index("--refresh")
        d = int(args[i + 1]) if len(args) > i + 1 and args[i + 1].isdigit() else 14
        refresh(d)
    elif "--merge" in args:
        i = args.index("--merge")
        if len(args) <= i + 1:
            raise SystemExit("укажи путь к базе: --merge ../other/hh_responses.db")
        merge(args[i + 1])
    elif "--report" in args:
        report()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
