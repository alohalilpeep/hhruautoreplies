"""
Статистика откликов с привязкой к версии резюме.

Идея: hh сам считает сводку на странице откликов (Все / Ожидание / Отказ /
Собеседование / Приглашение). Снимаем её слепком, записываем вместе с текущей
версией резюме из .env — и дальше видно, как менялись цифры между версиями.

    venv/bin/python hh_stats.py --snapshot              снять слепок
    venv/bin/python hh_stats.py --snapshot "правка 2.0" слепок с заметкой
    venv/bin/python hh_stats.py                         отчёт
    venv/bin/python hh_stats.py --daily                 отклики по дням
    venv/bin/python hh_stats.py --collect               снять статусы с hh и разложить по когортам
    venv/bin/python hh_stats.py --cohorts               отчёт по когортам

Про даты честно: в списке hh дата — это дата ОТКЛИКА, а не дата отказа.
Поэтому «сколько отказов пришло во вторник» ретроспективно не построить,
это считается разницей между слепками. Снимай слепок раз в день.
"""
import datetime as dt
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import hh_autoapply as hh
from hh_autoapply import init_db
from playwright.sync_api import sync_playwright

NEGOTIATIONS = "https://hh.ru/applicant/negotiations"

# вкладка hh -> колонка в базе
TABS = {
    "tab_filter_all": "total",
    "tab_filter_invitation": "invitation",
    "tab_filter_interview": "interview",
    "tab_filter_hired": "hired",
    "tab_filter_awaiting": "awaiting",
    "tab_filter_discard": "discard",
}


def already_open():
    """Профиль уже открыт? Тогда его нельзя закрывать в конце: скорее всего
    им в этот момент пользуется человек или другой прогон."""
    import requests
    try:
        r = requests.post(f"{hh.BIT_API}/browser/ports", json={},
                          headers=hh._headers(), timeout=10).json()
        return hh.PROFILE_ID in (r.get("data") or {})
    except Exception:
        return False


def ensure_table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS stats_snapshots (
        ts TEXT PRIMARY KEY, resume_version TEXT,
        total INTEGER, invitation INTEGER, interview INTEGER,
        hired INTEGER, awaiting INTEGER, discard INTEGER,
        applied_own INTEGER, note TEXT)""")
    db.commit()


def read_counters(page):
    """Числа со вкладок. Пустая вкладка означает ноль, а не отсутствие данных."""
    raw = page.evaluate("""() => {
        const out = {};
        for (const e of document.querySelectorAll('[data-qa^="tab_filter_"]')) {
            out[e.getAttribute('data-qa')] = (e.innerText||'').replace(/\\s+/g,' ').trim();
        }
        return out;
    }""")
    counts = {}
    for qa, col in TABS.items():
        text = raw.get(qa, "")
        m = re.search(r"(\d[\d\s ]*)$", text)
        counts[col] = int(re.sub(r"\D", "", m.group(1))) if m else 0
    return counts


def snapshot(note=""):
    db = init_db()
    ensure_table(db)
    was_open = already_open()
    ws = hh.open_profile()
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(ws)
            ctx = browser.contexts[0]
            page = ctx.new_page()
            page.set_default_navigation_timeout(90000)
            page.goto(NEGOTIATIONS, wait_until="commit")
            page.wait_for_timeout(9000)
            counts = read_counters(page)
            page.close()
    finally:
        if not was_open:
            hh.close_profile()

    own = db.execute(
        "SELECT COUNT(*) FROM responses WHERE status IN ('applied','answered')"
    ).fetchone()[0]
    ts = dt.datetime.now().isoformat(timespec="seconds")
    db.execute(
        "INSERT OR REPLACE INTO stats_snapshots VALUES (?,?,?,?,?,?,?,?,?,?)",
        (ts, hh.RESUME_VERSION, counts["total"], counts["invitation"],
         counts["interview"], counts["hired"], counts["awaiting"],
         counts["discard"], own, note))
    db.commit()
    print(f"слепок {ts}  резюме {hh.RESUME_VERSION}"
          + (f"  — {note}" if note else ""))
    for k, v in counts.items():
        print(f"  {k:12s} {v}")
    print(f"  {'наши отклики':12s} {own}")
    report(db)


def report(db=None):
    db = db or init_db()
    ensure_table(db)
    rows = db.execute(
        "SELECT ts, resume_version, total, invitation, interview, hired, "
        "awaiting, discard, applied_own, note FROM stats_snapshots ORDER BY ts"
    ).fetchall()
    if not rows:
        print("слепков ещё нет: venv/bin/python hh_stats.py --snapshot")
        return

    print(f"\n{'дата':16s} {'рез':5s} {'всего':>6s} {'ожид':>6s} "
          f"{'отказ':>6s} {'собес':>6s} {'пригл':>6s}  заметка")
    prev = None
    for (ts, ver, total, inv, interview, hired, awaiting, discard,
         own, note) in rows:
        d = ""
        if prev and prev[1] == ver:
            d = (f"  (+{total - prev[2]} откл, +{discard - prev[7]} отк, "
                 f"+{interview - prev[4]} собес)")
        print(f"{ts[:16]:16s} {ver:5s} {total:6d} {awaiting:6d} "
              f"{discard:6d} {interview:6d} {inv:6d}  {note or ''}{d}")
        prev = (ts, ver, total, inv, interview, hired, awaiting, discard)

    print("\n=== по версиям резюме ===")
    for ver, in db.execute(
            "SELECT DISTINCT resume_version FROM stats_snapshots "
            "ORDER BY resume_version"):
        first = db.execute(
            "SELECT total, discard, interview, invitation FROM stats_snapshots "
            "WHERE resume_version=? ORDER BY ts LIMIT 1", (ver,)).fetchone()
        last = db.execute(
            "SELECT total, discard, interview, invitation FROM stats_snapshots "
            "WHERE resume_version=? ORDER BY ts DESC LIMIT 1", (ver,)).fetchone()
        sent = last[0] - first[0]
        disc = last[1] - first[1]
        intr = last[2] - first[2]
        invt = last[3] - first[3]
        print(f"\n  версия {ver}")
        if sent <= 0:
            print("    слепков мало, прироста ещё нет")
            continue
        print(f"    откликов за период: {sent}")
        print(f"    отказов:            {disc:3d}  ({disc / sent * 100:.0f}%)")
        print(f"    собеседований:      {intr:3d}  ({intr / sent * 100:.0f}%)")
        print(f"    приглашений:        {invt:3d}  ({invt / sent * 100:.0f}%)")

    print("\n=== сопроводительное письмо ===")
    rows = db.execute("""
        SELECT resume_version,
               SUM(CASE WHEN letter_sent=1 THEN 1 ELSE 0 END),
               SUM(CASE WHEN letter_sent=0 THEN 1 ELSE 0 END),
               SUM(CASE WHEN letter_sent IS NULL THEN 1 ELSE 0 END)
        FROM responses WHERE status IN ('applied','answered')
        GROUP BY resume_version ORDER BY resume_version""").fetchall()
    for ver, with_l, without_l, unknown in rows:
        print(f"  версия {ver or '?'}: с письмом {with_l}, без письма {without_l}"
              + (f", неизвестно {unknown}" if unknown else ""))
    if any(r[3] for r in rows):
        print("  «неизвестно» — отклики до того, как признак начали писать")

    print("\nтекущая версия резюме в .env:", hh.RESUME_VERSION)


def daily(db=None):
    db = db or init_db()
    print("=== наши отклики по дням (из базы) ===")
    rows = db.execute(
        "SELECT substr(ts,1,10) d, resume_version, COUNT(*) FROM responses "
        "WHERE status IN ('applied','answered') "
        "GROUP BY d, resume_version ORDER BY d").fetchall()
    if not rows:
        print("  откликов ещё нет")
        return
    peak = max(n for *_, n in rows) or 1
    for d, ver, n in rows:
        bar = "█" * max(1, round(n / peak * 44))
        print(f"  {d}  рез {ver or '?':4s} {n:4d}  {bar}")

    print("\n=== прирост отказов и собеседований между слепками ===")
    snaps = db.execute(
        "SELECT ts, discard, interview FROM stats_snapshots ORDER BY ts"
    ).fetchall()
    if len(snaps) < 2:
        print("  нужно минимум два слепка, снимай раз в день")
        return
    for (t1, d1, i1), (t2, d2, i2) in zip(snaps, snaps[1:]):
        print(f"  {t1[:10]} → {t2[:10]}   отказов +{d2 - d1}, "
              f"собеседований +{i2 - i1}")


def main():
    args = sys.argv[1:]
    if "--snapshot" in args:
        i = args.index("--snapshot")
        snapshot(" ".join(args[i + 1:]).strip())
    elif "--collect" in args:
        collect()
    elif "--cohorts" in args:
        cohorts()
    elif "--daily" in args:
        daily()
    else:
        report()



# ============ когортный подсчёт ============
# Статус каждого отклика с hh привязывается к версии резюме, под которой он
# ушёл. Точного id вакансии в списке откликов нет — заголовок там простой span,
# ссылка только на работодателя. Поэтому сопоставляем по паре
# «компания + название вакансии». Дубли помечаются отдельно и не считаются.
STATUS_MAP = {
    "отказ": "discard",
    "приглашение": "invitation",
    "собеседование": "interview",
    "выход на работу": "hired",
    "не просмотрен": "not_viewed",
    "просмотрен": "viewed",
}


def _key(company, title):
    def norm(x):
        x = (x or "").lower().replace("ё", "е")
        x = re.sub(r"[^\w\s]", " ", x)
        return re.sub(r"\s+", " ", x).strip()
    return norm(company), norm(title)


def collect_negotiations(page, max_pages=25):
    """Все записи из списка откликов, со статусом и датой."""
    items, seen_pages = [], 0
    for n in range(max_pages):
        page.goto(f"{NEGOTIATIONS}?page={n}", wait_until="commit")
        page.wait_for_timeout(5000)
        chunk = page.evaluate("""() => [...document.querySelectorAll('[data-qa="negotiations-item"]')]
            .map(el => {
                const q = s => { const x = el.querySelector(s); return x ? (x.innerText||'').trim() : ''; };
                const tag = [...el.querySelectorAll('[data-qa^="negotiations-tag"]')]
                            .map(e => (e.innerText||'').trim()).filter(Boolean)[0] || '';
                return {tag, company: q('[data-qa="negotiations-item-company"]'),
                        title: q('[data-qa="negotiations-item-vacancy"]'),
                        date: q('[data-qa="negotiations-item-date"]')};
            })""")
        if not chunk:
            break
        items.extend(chunk)
        seen_pages += 1
    print(f"страниц откликов прочитано: {seen_pages}, записей: {len(items)}")
    return items


def match_and_store(db, items):
    """Разложить статусы hh по нашим откликам."""
    cols = {r[1] for r in db.execute("PRAGMA table_info(responses)")}
    if "hh_status" not in cols:
        db.execute("ALTER TABLE responses ADD COLUMN hh_status TEXT")
        db.execute("ALTER TABLE responses ADD COLUMN hh_status_ts TEXT")

    ours = {}
    for rid, company, title in db.execute(
            "SELECT id, company, title FROM responses "
            "WHERE status IN ('applied','answered')"):
        ours.setdefault(_key(company, title), []).append(rid)

    ts = dt.datetime.now().isoformat(timespec="seconds")
    matched, ambiguous, unmatched = 0, 0, 0
    for it in items:
        # hh ставит в тегах неразрывный пробел: «Не\xa0просмотрен»
        tag = re.sub(r"\s+", " ", (it["tag"] or "").replace("\xa0", " ")).strip()
        st = STATUS_MAP.get(tag.lower())
        if not st:
            continue
        ids = ours.get(_key(it["company"], it["title"]))
        if not ids:
            unmatched += 1
            continue
        if len(ids) > 1:
            ambiguous += 1          # один и тот же заголовок откликнут дважды
            continue
        db.execute("UPDATE responses SET hh_status=?, hh_status_ts=? WHERE id=?",
                   (st, ts, ids[0]))
        matched += 1
    db.commit()
    print(f"сопоставлено: {matched}, неоднозначных (дубли): {ambiguous}, "
          f"не наших: {unmatched}")


def cohorts(db=None):
    db = db or init_db()
    cols = {r[1] for r in db.execute("PRAGMA table_info(responses)")}
    if "hh_status" not in cols:
        print("данных нет: сначала venv/bin/python hh_stats.py --collect")
        return
    ORDER = ["hired", "invitation", "interview", "discard", "viewed",
             "not_viewed", None]
    NAMES = {"hired": "выход на работу", "invitation": "приглашение",
             "interview": "собеседование", "discard": "отказ",
             "viewed": "просмотрен", "not_viewed": "не просмотрен",
             None: "нет данных"}
    print("\n=== когорты по версиям резюме ===")
    for (ver,) in db.execute(
            "SELECT DISTINCT resume_version FROM responses "
            "WHERE status IN ('applied','answered') ORDER BY resume_version"):
        rows = dict(db.execute(
            "SELECT hh_status, COUNT(*) FROM responses "
            "WHERE status IN ('applied','answered') AND resume_version=? "
            "GROUP BY hh_status", (ver,)).fetchall())
        total = sum(rows.values())
        print(f"\n  резюме {ver} — {total} откликов")
        for st in ORDER:
            n = rows.get(st, 0)
            if not n:
                continue
            print(f"    {NAMES[st]:16s} {n:4d}  {n / total * 100:5.1f}%")


def collect():
    db = init_db()
    was_open = already_open()
    ws = hh.open_profile()
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(ws)
            ctx = browser.contexts[0]
            page = ctx.new_page()
            page.set_default_navigation_timeout(90000)
            items = collect_negotiations(page)
            page.close()
    finally:
        if not was_open:
            hh.close_profile()
    match_and_store(db, items)
    cohorts(db)

if __name__ == "__main__":
    main()
