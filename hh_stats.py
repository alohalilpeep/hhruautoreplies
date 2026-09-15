"""
Статистика откликов с привязкой к версии резюме.

Идея: hh сам считает сводку на странице откликов (Все / Ожидание / Отказ /
Собеседование / Приглашение). Снимаем её слепком, записываем вместе с текущей
версией резюме из .env — и дальше видно, как менялись цифры между версиями.

    venv/bin/python hh_stats.py --snapshot              снять слепок
    venv/bin/python hh_stats.py --snapshot "правка 2.0" слепок с заметкой
    venv/bin/python hh_stats.py                         отчёт
    venv/bin/python hh_stats.py --daily                 отклики по дням

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
    elif "--daily" in args:
        daily()
    else:
        report()


if __name__ == "__main__":
    main()
