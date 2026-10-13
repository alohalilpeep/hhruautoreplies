"""Выгрузка компаний, которые нанимают инфраструктурных инженеров, — для B2B.

    python scripts/b2b_companies.py

Сводит в одну строку на компанию всё, что накопилось у трёх аккаунтов hh и на
getmatch: сколько инфраструктурных вакансий, какие, когда последняя, вилка
зарплат (сигнал бюджета), формат, главный стек, ссылки, история откликов
(собеседование, отказ) и контакты HR из чатов. Сортировка — по числу
вакансий: кто нанимает больше инженеров, тому нужнее подрядчик.

Пишет exports/b2b_companies_<дата>.csv в папке первого аккаунта: разделитель
«;», UTF-8 с BOM — открывается в Excel и Google Таблицах как есть.
Ничего не меняет в базах.
"""
import collections
import csv
import datetime as dt
import re
import sqlite3
from pathlib import Path

BASE = Path("/Users/mr_macbook/Documents")
ACCS = ("hh_autoreply", "hh_autoreply_2", "hh_autoreply_3")
OUT_DIR = BASE / "hh_autoreply" / "exports"

AGENCY_RE = re.compile(r"кадров|рекрут|recruit|\bhr\b|агентств|staff|персонал|"
                       r"headhunt|getmatch agency|подбор", re.I)
HIDDEN_RE = re.compile(r"^компания скрыта", re.I)
TG_RE = re.compile(r"(?<![\w.])@([A-Za-z][A-Za-z0-9_]{4,31})")
TG_LINK_RE = re.compile(r"t\.me/([A-Za-z][A-Za-z0-9_]{4,31})")
PHONE_RE = re.compile(r"(?:\+7|8)[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")
MAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
OWN_TG = {"artyom_work_devops", "artyom_devops", "artyom_worker"}   # свои ники
OWN_PHONES = {"9933233805", "9153979869", "9999904800"}


def norm_company(name):
    return re.sub(r"\s+", " ", (name or "").replace("\xa0", " ")).strip()


def key_of(name):
    """Ключ для склейки: «ООО Ромашка», «Ромашка» и «РОМАШКА» — одна компания."""
    k = norm_company(name).lower()
    k = re.sub(r"^(ооо|оао|зао|пао|ао|ип|гку|гуп|нао|ткп)\s+|[«»\"']", "", k)
    return re.sub(r"[^\w]+", " ", k).strip()


def blacklist():
    try:
        lines = (BASE / "hh_autoreply" / "blacklist.txt").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [l.strip().lower() for l in lines if l.strip() and not l.startswith("#")]


def main():
    comp = collections.defaultdict(lambda: {
        "names": collections.Counter(), "vacancies": {}, "sources": set(),
        "salary_from": [], "salary_to": [], "formats": collections.Counter(),
        "skills": collections.Counter(), "experience": collections.Counter(),
        "last": "", "applied": 0, "interview": 0, "discard": 0,
        "tg": set(), "phones": set(), "mails": set(), "gm_url": ""})

    for acc in ACCS:
        db = sqlite3.connect(BASE / acc / "hh_responses.db")
        cols = {r[1] for r in db.execute("PRAGMA table_info(responses)")}
        has_status = "hh_status" in cols
        facts = {r[0]: r[1:] for r in db.execute(
            "SELECT vacancy_id, salary_from, salary_to, experience, work_format, published_at "
            "FROM vacancy_facts")}
        skills = collections.defaultdict(list)
        for vid, skill, st in db.execute("SELECT vacancy_id, skill, status FROM vacancy_skills"):
            if st != "stale" and skill and "?" not in skill and len(skill) <= 30:
                skills[vid].append(skill)
        q = ("SELECT id, url, title, company, status, ts" +
             (", hh_status" if has_status else ", NULL") + " FROM responses")
        for vid, url, title, company, status, ts, hh_status in db.execute(q):
            if not company:
                continue
            c = comp[key_of(company)]
            c["names"][norm_company(company)] += 1
            c["sources"].add("hh")
            c["vacancies"].setdefault(vid, (title, url))
            if status in ("applied", "answered"):
                c["applied"] += 1
            if hh_status == "interview":
                c["interview"] += 1
            if hh_status == "discard":
                c["discard"] += 1
            f = facts.get(vid)
            if f:
                s_from, s_to, exp, fmt, pub = f
                if s_from:
                    c["salary_from"].append(s_from)
                if s_to:
                    c["salary_to"].append(s_to)
                if fmt:
                    c["formats"][fmt] += 1
                if exp:
                    c["experience"][re.sub(r"^Опыт\s+", "", exp)] += 1
                ts = max(ts or "", pub or "")
            c["last"] = max(c["last"], ts or "")
            for s in skills.get(vid, []):
                c["skills"][s] += 1
        # контакты HR из чатов: только реплики собеседника, свои ники и номера — мимо
        for company, text, kind in db.execute(
                "SELECT company, text, kind FROM chat_messages WHERE company IS NOT NULL"):
            c = comp.get(key_of(company))
            if c is None or kind in ("автоответ", "бот"):
                continue
            for h in TG_RE.findall(text or "") + TG_LINK_RE.findall(text or ""):
                if h.lower() not in OWN_TG and not h.lower().endswith(("gmail", "mail", "yandex")):
                    c["tg"].add("@" + h)
            for ph in PHONE_RE.findall(text or ""):
                digits = re.sub(r"\D", "", ph)[-10:]
                if digits not in OWN_PHONES:
                    c["phones"].add("+7" + digits)
            for m in MAIL_RE.findall(text or ""):
                if not m.lower().startswith("artyom."):
                    c["mails"].add(m)
            if kind == "приглашение":
                c["interview"] = max(c["interview"], 1)
        if "gm_vacancies" in {r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}:
            gm_comp = {k: u for k, u in db.execute("SELECT key, url FROM gm_companies")}
            for vid, url, pos, ckey, company, pub, s_from, s_to, fmts, applied in db.execute(
                    "SELECT id, url, position, company_key, company, published_at, salary_from, "
                    "salary_to, formats, applied FROM gm_vacancies"):
                if not company:
                    continue
                c = comp[key_of(company)]
                c["names"][norm_company(company)] += 1
                c["sources"].add("getmatch")
                c["vacancies"].setdefault(f"gm{vid}", (pos, url))
                c["gm_url"] = c["gm_url"] or (gm_comp.get(ckey) or "")
                if s_from:
                    c["salary_from"].append(s_from)
                if s_to:
                    c["salary_to"].append(s_to)
                for f in (fmts or "").split(","):
                    if f:
                        c["formats"][f] += 1
                c["last"] = max(c["last"], pub or "")
                c["applied"] += 1 if applied else 0

    stop = blacklist()
    rows = []
    for key, c in comp.items():
        name = c["names"].most_common(1)[0][0]
        if not key or HIDDEN_RE.match(name):
            continue
        vac = list(c["vacancies"].values())
        sal_from = min(c["salary_from"]) if c["salary_from"] else ""
        sal_to = max(c["salary_to"]) if c["salary_to"] else ""
        rows.append({
            "Компания": name,
            "Вакансий инфраструктуры": len(vac),
            "Последняя вакансия": (c["last"] or "")[:10],
            "Вакансии (до 5)": " | ".join(dict.fromkeys(t for t, _ in vac if t))[:400],
            "Зарплата от": sal_from,
            "Зарплата до": sal_to,
            "Формат": ", ".join(f for f, _ in c["formats"].most_common(3)),
            "Опыт": ", ".join(e for e, _ in c["experience"].most_common(2)),
            "Стек (топ)": ", ".join(s for s, _ in c["skills"].most_common(8)),
            "Источник": ", ".join(sorted(c["sources"])),
            "Откликов": c["applied"],
            "Было собеседование": "да" if c["interview"] else "",
            "Отказ": "да" if c["discard"] else "",
            "Телеграм HR": ", ".join(sorted(c["tg"])),
            "Телефон HR": ", ".join(sorted(c["phones"])),
            "Почта HR": ", ".join(sorted(c["mails"])),
            "Кадровое агентство": "да" if AGENCY_RE.search(name) else "",
            "В стоп-листе": "да" if any(s in name.lower() for s in stop) else "",
            "Страница на getmatch": c["gm_url"],
            "Ссылка на вакансию": next((u for _, u in vac if u), ""),
        })
    rows.sort(key=lambda r: (-r["Вакансий инфраструктуры"], r["Последняя вакансия"]), reverse=False)
    rows.sort(key=lambda r: (r["Вакансий инфраструктуры"], r["Последняя вакансия"]), reverse=True)

    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / f"b2b_companies_{dt.date.today().isoformat()}.csv"
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()), delimiter=";")
        w.writeheader()
        w.writerows(rows)
    agencies = sum(1 for r in rows if r["Кадровое агентство"])
    with_contacts = sum(1 for r in rows if r["Телеграм HR"] or r["Телефон HR"] or r["Почта HR"])
    print(f"компаний: {len(rows)}, из них кадровых агентств {agencies}, "
          f"с контактами HR {with_contacts}, с 3+ вакансиями "
          f"{sum(1 for r in rows if r['Вакансий инфраструктуры'] >= 3)}")
    print(f"файл: {out}")
    for r in rows[:15]:
        print(f"  {r['Вакансий инфраструктуры']:3d}  {r['Компания'][:40]:40s} "
              f"{r['Последняя вакансия']}  {r['Зарплата от'] or ''}–{r['Зарплата до'] or ''}  "
              f"{r['Формат'][:30]}")


if __name__ == "__main__":
    main()
