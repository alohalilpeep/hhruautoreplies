"""Фильтр, обогащение и скоринг базы компаний для холодного аутрича DevOps-услуг.

    python scripts/b2b_score.py step1     предфильтр по названию и вакансиям
    python scripts/b2b_score.py step2     ИНН через DaData + численность/доходы из fns.db
    python scripts/b2b_score.py step2b    ИНН с сайтов для «ручных» (сайты — sites_out_*.json)
    python scripts/b2b_score.py step3     дофильтр, баллы, оффер, волны

Читает exports/b2b_2026-10-13.xlsx (лист «Все»), исходник не меняет.
Промежуточные результаты — exports/b2b_work/.
"""
import json
import random
import re
import sys
from pathlib import Path

from openpyxl import load_workbook

EXPORTS = Path("/Users/mr_macbook/Documents/hh_autoreply/exports")
SRC = EXPORTS / "b2b_2026-10-13.xlsx"
WORK = EXPORTS / "b2b_work"


def low(s):
    return (s or "").lower().replace("ё", "е")


def rx(*words):
    return re.compile("|".join(words), re.I)


# ---------- списки исключений: проверяются в этом порядке ----------
# Посредники и вендоры — по точным брендам, раньше «гигантов»: РТК-ЦОД —
# вендор, хотя и «дочка» Ростелекома.
INTERMEDIARY = rx(
    r"nitka", r"\bibs\b", r"bell integrator", r"бэлл интегратор", r"usetech", r"ай-теко",
    r"andersen", r"neoflex", r"инфосистемы джет", r"jet infosystems", r"\bкрок\b", r"\bcroc\b",
    r"agima", r"hilbert", r"employcity", r"syberry", r"alta personnel", r"\bантал\b", r"antal",
    r"devhunt", r"easy ?hunt", r"getmatch", r"offer ?now", r"лига цифровой экономики",
    r"сателл", r"\bicl\b", r"ancor", r"анкор", r"itransition", r"it_one", r"it one\b",
    r"онланта", r"ланит", r"\bт1\b", r"\bt1\b", r"технократия", r"\bkts\b", r"emphasoft",
    r"live typing", r"кадров", r"рекрут", r"recruit", r"headhunt", r"\bhr\b", r"агентств",
    r"\bstaff", r"персонал", r"аутстаф", r"outstaff", r"аутсорс", r"outsourc", r"подбор",
    r"talent", r"hunt")
# неочевидные посредники: интеграторы и консалтинг без явного слова в названии
MAYBE_INTERMEDIARY = rx(r"интеграц", r"integrat", r"консалт", r"consult", r"solutions?\b",
                        r"софт ?лаб", r"soft ?lab", r"digital agency", r"веб-студ", r"студия")
VENDOR = rx(
    r"cloud\.ru", r"timeweb", r"таймвэб", r"т1 облако", r"\bастра\b", r"astra linux",
    r"группа компаний астра", r"orion soft", r"yadro", r"ядро", r"edge ?центр", r"edgecenter",
    r"ртк-цод", r"ddos-guard", r"stormwall", r"servicepipe", r"ngenix", r"servers\.com",
    r"ispsystem", r"\bсолар\b", r"solar\b", r"bi\.zone", r"positive technologies",
    r"позитив текнолоджиз", r"касперск", r"kaspersky", r"swordfish", r"group-ib", r"гарда",
    r"r-vision", r"р-вижн", r"infowatch", r"инфовотч", r"аренадата", r"arenadata", r"криптонит",
    r"qtech", r"\bбулат\b", r"selectel", r"селектел", r"\breg\.ru", r"рег\.ру", r"beget",
    r"mws\b", r"mts web services", r"yandex cloud", r"vk cloud", r"sbercloud", r"red ?os",
    r"ред ?софт", r"базальт", r"\bsber\b ?cloud", r"dataline", r"даталайн", r"ixcellerate",
    r"3data", r"linxdatacenter", r"\bкод безопасности\b", r"ufo ?team", r"ростелеком-солар")
GIANT = rx(
    # банки топ-20
    r"\bсбер", r"\bsber", r"\bвтб\b", r"\bvtb\b", r"альфа-банк", r"альфа банк", r"alfa-?bank",
    r"газпромбанк", r"россельхозбанк", r"\bрсхб", r"т-банк", r"тинькофф", r"tinkoff",
    r"московский кредитный", r"\bмкб\b", r"совкомбанк", r"открытие", r"райффайзен",
    r"raiffeisen", r"росбанк", r"промсвязьбанк", r"\bпсб\b", r"уралсиб", r"почта банк",
    r"ак барс", r"юникредит", r"unicredit", r"дом\.рф", r"мтс банк", r"хоум кредит",
    r"home credit", r"ренессанс банк", r"банк санкт-петербург", r"\bотп банк", r"otp bank",
    # телеком и «дочки»
    r"\bмтс\b", r"\bmts\b", r"билайн", r"beeline", r"вымпелком", r"\bt2\b", r"\bтеле2",
    r"tele2", r"ростелеком", r"rostelecom", r"\bртк\b", r"мегафон", r"megafon", r"ртлабс",
    # бигтех и ритейл
    r"яндекс", r"yandex", r"\bvk\b", r"вконтакте", r"\bozon", r"озон", r"wildberries",
    r"\brwb\b", r"\bwb\b", r"вайлдберриз", r"\bx5\b", r"магнит", r"magnit", r"лента",
    r"пятерочк", r"перекрест", r"авито", r"avito", r"kaspi",
    # госкорпорации и холдинги
    r"ростех", r"росатом", r"роскосмос", r"\bржд\b", r"аэрофлот", r"почта россии",
    r"норникель", r"норильск", r"русал", r"северсталь", r"евраз", r"газпром", r"лукойл",
    r"роснефть", r"сибур", r"транснефть", r"нлмк", r"\bммк\b", r"металлоинвест", r"фосагро",
    r"уралкалий", r"сургутнефтегаз", r"татнефть", r"\bполюс\b", r"мечел", r"\bсуэк\b",
    r"интер рао", r"россети", r"русгидро", r"\bафк система", r"\bсистема\b",
    # фастфуд
    r"вкусно.{0,3}и точка", r"\bkfc\b", r"rostic", r"burger king", r"бургер кинг")
GOV = rx(
    r"^\s*(гку|гуп|фгуп|гау|фку|фбун|гбу|фгбу|фгау|мку|мбу|гкоу|фгбоу)\b",
    r"\b(гку|гуп|фгуп|гау|фку|фбун|фгбу|фгау)\b", r"ано .*федеральн", r"федеральн\w* .*ано",
    r"правительств", r"министерств", r"департамент", r"\bнии\b", r"научно-исследовательск",
    r"\bнпо\b", r"концерн", r"оборон", r"метрополитен", r"росгвард", r"налогов\w* служб",
    r"федеральн\w* (служб|агентств|центр)", r"инфогород", r"мосгортранс", r"мфц",
    r"гнивц", r"фонд пенсионного", r"социального страхования", r"\bфнс\b", r"\bфсин\b",
    r"гознак")
LEGAL = re.compile(r"^\s*ип\s+|^индивидуальный предприниматель", re.I)
FIO = re.compile(r"^[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+(?:вич|вна|ична|ич)$")
# «Не DevOps» — только если все вакансии про поддержку пользователей, телефонию
# или 1С. Системных и сетевых администраторов оставляем (решение 13.10.2026).
NOT_DEVOPS = rx(r"поддержк", r"support", r"helpdesk", r"хелпдеск", r"телефон", r"\bip-?телефон",
                r"\bаты\b", r"\b1с\b", r"\b1c\b", r"эникей", r"оператор", r"диспетчер")


# Слова, при которых вакансия остаётся, даже если в ней 1С или поддержка:
# «DevOps-инженер (1С)», «SRE (1С)», «Администратор 1С / Linux» — это наш клиент.
KEEP_WORDS = rx(r"devops", r"\bsre\b", r"reliability", r"platform", r"платформ", r"kubernetes",
                r"\bk8s\b", r"devsecops", r"mlops", r"ci/cd", r"инфраструктур", r"cloud", r"облач",
                r"linux", r"сервер", r"сетев", r"network", r"виртуал")


def all_not_devops(titles):
    return bool(titles) and all(NOT_DEVOPS.search(t) and not KEEP_WORDS.search(t)
                                for t in titles)


LOOKALIKE = str.maketrans("асеорхуктмнв", "aceopxyktmhb")


def unmix(s):
    """«Сloud.ru» с кириллической С → «cloud.ru»: в словах, где есть латиница,
    кириллические двойники латинских букв меняем на латиницу."""
    return re.sub(r"\S+", lambda m: m.group(0).translate(LOOKALIKE)
                  if re.search(r"[a-z]", m.group(0)) else m.group(0), s)


def classify(name, titles):
    n = low(name)
    variants = (n, unmix(n))
    if LEGAL.search(name) or FIO.match(name.strip()):
        return "посредник"
    if any(INTERMEDIARY.search(v) for v in variants):
        return "посредник"
    if any(VENDOR.search(v) for v in variants):
        return "инфра-вендор"
    if any(GIANT.search(v) for v in variants):
        return "гигант"
    if GOV.search(n):
        return "гос"
    if all_not_devops(titles):
        return "не DevOps"
    if MAYBE_INTERMEDIARY.search(n):
        return "посредник?"
    return ""


# ---------- склейка дублей ----------
ALIASES = {"мкб": "московскийкредитныйбанк"}
DROP_WORDS = re.compile(
    r"\b(ооо|оао|зао|пао|ао|нао|ип|llc|inc|ltd|limited|gmbh|corp|corporation|group|груп+а?|"
    r"гк|georgia|грузия|rnd|r&d|ру|ru|com|io|tech|техн?|team|тим)\b", re.I)


def dedupe_key(name):
    k = low(name)
    k = re.sub(r"[«»\"'`.,:;()\-–—/&]+", " ", k)
    k = DROP_WORDS.sub(" ", k)
    k = re.sub(r"\s+", "", k)
    return ALIASES.get(k, k)


def read_src():
    wb = load_workbook(SRC, read_only=True, data_only=True)
    ws = wb["Все"]
    rows = list(ws.iter_rows(values_only=True))
    head = [h for h in rows[0]]
    out = []
    for r in rows[1:]:
        d = dict(zip(head, r))
        if d.get("Компания"):
            out.append(d)
    return out


def merge(rows):
    groups = {}
    for d in rows:
        groups.setdefault(dedupe_key(d["Компания"]), []).append(d)
    merged = []
    for key, items in groups.items():
        items.sort(key=lambda d: -(d.get("Вакансий инфраструктуры") or 0))
        base = dict(items[0])
        titles = []
        for d in items:
            for t in (d.get("Вакансии (до 5)") or "").split("\n"):
                if t.strip() and t.strip() not in titles:
                    titles.append(t.strip())
        stack = []
        for d in items:
            for s in (d.get("Стек") or "").split(","):
                if s.strip() and s.strip() not in stack:
                    stack.append(s.strip())
        dates_last = [d["Последняя инфра-вакансия"] for d in items if d.get("Последняя инфра-вакансия")]
        dates_old = [d["Самая старая открытая"] for d in items if d.get("Самая старая открытая")]
        sal_from = [d["Зарплата от"] for d in items if d.get("Зарплата от")]
        sal_to = [d["Зарплата до"] for d in items if d.get("Зарплата до")]
        base.update({
            "Вакансий инфраструктуры": sum(d.get("Вакансий инфраструктуры") or 0 for d in items),
            "Вакансии (до 5)": "\n".join(titles[:5]),
            "_все названия": titles,
            "Стек": ", ".join(stack[:10]),
            "Последняя инфра-вакансия": max(dates_last) if dates_last else None,
            "Самая старая открытая": min(dates_old) if dates_old else None,
            "Зарплата от": min(sal_from) if sal_from else None,
            "Зарплата до": max(sal_to) if sal_to else None,
            "Дубль с": ", ".join(d["Компания"] for d in items[1:]),
        })
        merged.append(base)
    return merged


def step1():
    rows = merge(read_src())
    for d in rows:
        d["Исключение"] = classify(d["Компания"], d["_все названия"])
    WORK.mkdir(exist_ok=True)
    out = WORK / "step1.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    from collections import Counter
    stat = Counter(d["Исключение"] or "оставлено" for d in rows)
    print(f"строк в «Все»: {len(read_src())}, после склейки дублей: {len(rows)}")
    for k, v in stat.most_common():
        print(f"  {k:14s} {v}")
    dups = [d for d in rows if d["Дубль с"]]
    print(f"\nсклеено дублей: {len(dups)}")
    for d in dups:
        print(f"  {d['Компания']}  ⟵  {d['Дубль с']}")
    excl = [d for d in rows if d["Исключение"] and d["Исключение"] != "посредник?"]
    random.seed(13)
    print("\n30 случайных исключённых:")
    for d in random.sample(excl, min(30, len(excl))):
        t = (d["Вакансии (до 5)"] or "").split("\n")[0][:45]
        print(f"  [{d['Исключение']}] {d['Компания'][:38]:38s} | {t}")
    maybe = [d for d in rows if d["Исключение"] == "посредник?"]
    print(f"\nвсе «посредник?» ({len(maybe)}):")
    for d in maybe:
        t = (d["Вакансии (до 5)"] or "").split("\n")[0][:45]
        print(f"  {d['Компания'][:40]:40s} | {t}")
    print(f"\nрезультат шага 1: {out}")


# ---------- шаг 2: обогащение через DaData ----------
DADATA = "https://suggestions.dadata.ru/suggestions/api/4_1/rs"
CACHE = WORK / "dadata_cache.json"
OPF_RE = re.compile(r"^(ооо|оао|зао|пао|ао|нао|ип|ткп|llc|ltd|inc)\s+|[«»\"']", re.I)


def dadata_key():
    env = Path("/Users/mr_macbook/Documents/hh_autoreply/.env").read_text()
    m = re.search(r"^DADATA_API_KEY=(\S+)", env, re.M)
    if not m:
        raise SystemExit("нет DADATA_API_KEY в .env первого аккаунта")
    return m.group(1)


def cached(cache, kind, query, call):
    k = f"{kind}:{query}"
    if k not in cache:
        cache[k] = call()
        CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        import time
        time.sleep(0.15)                       # лимит DaData — 20 запросов в секунду
    return cache[k]


FNS_DB = WORK / "fns.db"
DEAD = {"LIQUIDATED", "LIQUIDATING", "BANKRUPT"}
# «Гознак. Информационные технологии», «СОГАЗ: г. Москва», «T2. IT и Digital», «… | IT»
DIVISION_SUFFIX = re.compile(
    r"(\.\s+[A-ZА-ЯЁ].*|:\s.*|\s+\|.*|,\s*(г\.|компания|сеть|работа).*|"
    r"\s+(направление|it|ит|it & digital|digital|r&d)\b.*)$", re.I)


TO_CYR = str.maketrans("aceopxyktmhb", "асеорхуктмнв")


def name_tokens(s):
    """Слова названия для сравнения; латинские двойники букв → кириллица,
    чтобы «1C-Рарус» совпал с «1С-РАРУС», а Okko — с «ОККО»."""
    s = OPF_RE.sub(" ", low(s)).translate(TO_CYR)
    return {t for t in re.split(r"[^\w]+", s) if len(t) > 1}


def queries(company):
    """Запросы к DaData по очереди: основной без суффикса подразделения, затем запасные."""
    base = DIVISION_SUFFIX.sub("", company).strip()
    out = [re.sub(r"\s*\(.*?\)", "", base).strip()]
    for p in re.findall(r"\((.*?)\)", company):          # «FunFlow (ООО Мобильные Игровые Решения)»
        out.append(p)
    out.append(re.sub(r"\.(ru|рф|com|io|tech)\b", "", out[0], flags=re.I))   # «Travelata.ru»
    out.append(" ".join(re.findall(r"[A-Za-z][A-Za-z0-9&-]+", out[0])))       # латинский бренд
    seen, res = set(), []
    for q in out:
        q = OPF_RE.sub(" ", q).strip(" .,-")
        if len(q) >= 2 and low(q) not in seen:
            seen.add(low(q))
            res.append(q)
    return res


def fns_row(db, inn):
    r = db.execute("SELECT staff, income FROM fns WHERE inn=?", (inn,)).fetchone()
    return r or (None, None)


def is_it(okved):
    return bool(re.match(r"6[23]\.", okved or ""))


def match(query, suggestions, db):
    """Выбор юрлица (правила 13.10.2026). Слова бренда — из очищенного запроса.
    «высокая» — ровно один действующий кандидат, в названии которого есть все слова
    бренда; численность не проверяем (< 10 — пометка, нет в fns.db — пустые данные).
    Иначе гибрид, только среди точных совпадений: численность ≥ 10 по ФНС; один с
    ОКВЭД 62/63 → «авто»; несколько → самый крупный, если он в 5 раз больше каждого
    из остальных. Кандидат без данных ФНС делает выбор неоднозначным. Регион не учитываем.
    Возвращает (лучший или None, уверенность, топ-3, причина)."""
    brand = name_tokens(query)
    pool, seen = [], set()
    for s in suggestions:
        d = s["data"]
        inn = d.get("inn")
        if not inn or inn in seen or d.get("type") == "INDIVIDUAL":
            continue
        seen.add(inn)
        if (d.get("state") or {}).get("status") in DEAD:
            continue
        cand = name_tokens(s["value"]) | name_tokens(((d.get("name") or {}).get("short")) or "")
        if not brand & cand:                              # подсказка DaData «по созвучию»
            continue
        staff, income = fns_row(db, inn)
        pool.append({"s": s, "staff": staff, "income": income, "exact": cand >= brand})
    pool.sort(key=lambda c: -(c["staff"] or 0))
    if not pool:
        return None, "ручная", [], "нет кандидатов"
    exact = [c for c in pool if c["exact"]]
    if len(exact) == 1:
        return exact[0], "высокая", pool[:3], ""
    if not exact:
        return None, "ручная", pool[:3], "нет точного совпадения названия"
    unknown = [c for c in exact if c["staff"] is None]
    sized = [c for c in exact if c["staff"] is not None and c["staff"] >= 10]
    it = [c for c in sized if is_it(c["s"]["data"].get("okved"))]
    it_unknown = [c for c in unknown if is_it(c["s"]["data"].get("okved"))]
    if len(it) == 1 and not it_unknown:
        return it[0], "авто", exact[:3], ""
    if sized and not unknown and sized[0]["staff"] >= 5 * max(c["staff"] for c in exact[1:]):
        return sized[0], "авто", exact[:3], ""
    return None, "ручная", exact[:3], "несколько однофамильцев, ни один не выделяется"


def okved_name(cache, key, code):
    if not code:
        return ""
    r = cached(cache, "okved", code, lambda: requests_post(
        key, "findById/okved2", {"query": code}))
    s = (r or {}).get("suggestions") or []
    return s[0]["data"].get("name", "") if s else ""


def requests_post(key, method, body):
    import requests
    r = requests.post(f"{DADATA}/{method}", json=body, timeout=20,
                      headers={"Authorization": f"Token {key}", "Content-Type": "application/json"})
    return r.json() if r.ok else {"error": r.status_code}


def region(d):
    return ((d.get("address") or {}).get("data") or {}).get("region_with_type") or ""


def describe(c):
    d = c["s"]["data"]
    return (f"{d.get('inn')} | {c['s']['value']} | {d.get('okved') or '—'} | "
            f"{c['staff']} чел. | {region(d) or '—'}")


def step2(limit=None):
    import sqlite3
    from collections import Counter
    rows = json.loads((WORK / "step1.json").read_text(encoding="utf-8"))
    key = dadata_key()
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    db = sqlite3.connect(FNS_DB)
    todo = [r for r in rows if r["Исключение"] in ("", "посредник?")]
    if limit:
        todo = todo[:limit]
    for i, r in enumerate(todo, 1):
        tried = []
        for q in queries(r["Компания"]):
            res = cached(cache, "party10", q, lambda: requests_post(
                key, "suggest/party", {"query": q, "count": 10}))
            tried.append(q)
            best, conf, top, why = match(q, (res or {}).get("suggestions") or [], db)
            if best or top:                 # нашли кандидатов — запасные запросы не нужны
                break
        r.update({"ИНН": "", "Юрлицо": "", "ОКВЭД код": "", "ОКВЭД расшифровка": "",
                  "Регион": "", "Статус": "", "Численность": None, "Доходы (ФНС)": None,
                  "Пометка": "", "Сайт": r.get("Сайт") or "",
                  "Уверенность сопоставления": conf, "Причина ручной проверки": why,
                  "Кандидаты (топ-3)": "\n".join(describe(c) for c in top) if not best else "",
                  "Запросы DaData": " / ".join(tried)})
        if best:
            d = best["s"]["data"]
            code = d.get("okved") or ""
            r.update({
                "ИНН": d.get("inn"), "Юрлицо": best["s"]["value"], "ОКВЭД код": code,
                "ОКВЭД расшифровка": okved_name(cache, key, code), "Регион": region(d),
                "Статус": "действующая", "Численность": best["staff"],
                "Доходы (ФНС)": best["income"],
                "Пометка": "нет данных ФНС по юрлицу" if best["staff"] is None else
                           "мало сотрудников в юрлице, возможно, штат в другом юрлице группы"
                           if best["staff"] < 10 else ""})
        if i % 50 == 0:
            print(f"  … {i}/{len(todo)}", flush=True)
    out = WORK / ("step2_sample.json" if limit else "step2.json")
    out.write_text(json.dumps(rows if not limit else todo, ensure_ascii=False, indent=1,
                              default=str), encoding="utf-8")

    stat = Counter(r["Уверенность сопоставления"] for r in todo)
    print(f"\nобогащено строк: {len(todo)}")
    for k in ("высокая", "авто", "ручная"):
        print(f"  {k:8s} {stat.get(k, 0)}")
    why = Counter(r["Причина ручной проверки"] for r in todo if r["Причина ручной проверки"])
    print("причины «ручная»:")
    for k, v in why.most_common():
        print(f"  {v:4d}  {k}")
    auto = [r for r in todo if r["Уверенность сопоставления"] == "авто"]
    random.seed(13)
    print("\n15 случайных «авто»:")
    for r in random.sample(auto, min(15, len(auto))):
        inc = f"{r['Доходы (ФНС)'] / 1e6:,.0f} млн".replace(",", " ") if r["Доходы (ФНС)"] else "—"
        print(f"  {r['Компания'][:32]:32s} → {r['Юрлицо'][:40]:40s} ИНН {r['ИНН']} | "
              f"{r['ОКВЭД код']:8s} | {r['Численность']:>5} чел. | {inc:>10s} | {r['Регион'][:20]}")
    print(f"\nрезультат: {out}")


# ---------- шаг 2.1: ИНН с сайта ----------
# Сайты нашёл веб-поиск (sites_out_*.json): {company, site, inn, legal, foreign, note}.
SITE_CACHE = WORK / "site_cache.json"
SITE_PAGES = ("", "/contacts", "/requisites", "/about", "/privacy", "/policy", "/oferta")
NOT_RF = "не РФ / нет юрлица РФ"
INN_RE = re.compile(r"(?:ИНН|INN)[\s:№/]*(?:КПП[\s:№]*\d{9}[\s,;/]*)?(\d{12}|\d{10})(?!\d)")
OGRN_RE = re.compile(r"(?:ОГРН(?:ИП)?|OGRN)[\s:№]*(\d{15}|\d{13})(?!\d)")
FOREIGN_RE = re.compile(r"\b(ТОО|TOO|БИН|LLC|Ltd\.?|Limited|GmbH|ЧК|Inc\.)\b|Kazakhstan|Казахстан"
                        r"|Узбекистан|Uzbekistan|Грузи[яи]|Georgia|Cyprus|Кипр|Армени[яи]", re.I)
CAPTCHA_RE = re.compile(r"captcha|cf-chl|challenge-platform|ddos-guard|are you a robot|"
                        r"подтвердите, что вы не робот", re.I)


def inn_ok(inn):
    """Контрольная сумма ИНН — отсекает случайные 10/12 цифр."""
    d = [int(c) for c in inn]
    def cs(w):
        return sum(a * b for a, b in zip(w, d)) % 11 % 10
    if len(d) == 10:
        return cs([2, 4, 10, 3, 5, 9, 4, 6, 8]) == d[9]
    if len(d) == 12:
        return (cs([7, 2, 4, 10, 3, 5, 9, 4, 6, 8]) == d[10]
                and cs([3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]) == d[11])
    return False


def fetch_site(cache, url):
    """Текст страницы (или маркер ошибки), с кэшем и паузой 1.5 с."""
    import time
    import requests
    if url in cache:
        return cache[url]
    try:
        r = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/128.0 Safari/537.36",
            "Accept-Language": "ru-RU,ru;q=0.9"})
        text = r.text if r.ok else f"#HTTP {r.status_code}"
        if CAPTCHA_RE.search(r.text[:20000]) and len(r.text) < 60000:
            text = "#CAPTCHA"
    except Exception as e:                         # noqa: BLE001 — сайт недоступен
        text = f"#ERR {type(e).__name__}"
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;|&#160;|\s+", " ", text)
    cache[url] = text[-60000:]                     # реквизиты почти всегда в футере
    SITE_CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    time.sleep(1.5)
    return cache[url]


def site_inn(cache, site):
    """(ИНН или ОГРН, признак иностранного юрлица, капча?) по главной и служебным страницам."""
    from collections import Counter
    base = re.match(r"https?://[^/]+", site.strip()).group(0)
    found, foreign = Counter(), False
    for i, p in enumerate(SITE_PAGES):
        text = fetch_site(cache, base + p)
        if text == "#CAPTCHA":
            return None, False, True
        if i == 0 and text.startswith("#"):        # главная не открылась — дальше не идём
            break
        for inn in INN_RE.findall(text):
            if inn_ok(inn):
                found[inn] += 1
        for ogrn in OGRN_RE.findall(text):
            found["ogrn:" + ogrn] += 1
        if FOREIGN_RE.search(text):
            foreign = True
        inns = [k for k in found if not k.startswith("ogrn:")]
        if inns and i >= 1:                        # главная + ещё одна страница — хватит
            break
    inns = [k for k, _ in found.most_common() if not k.startswith("ogrn:")]
    if inns:
        return inns[0], foreign, False
    ogrns = [k[5:] for k, _ in found.most_common() if k.startswith("ogrn:")]
    return (ogrns[0] if ogrns else None), foreign, False


TRANSLIT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                    ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n",
                     "o", "p", "r", "s", "t", "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "",
                     "e", "yu", "ya"]))


def guess_sites(company):
    """Домены-кандидаты, когда веб-поиск до компании не дошёл: бренд латиницей
    (как есть или транслитом) + .ru / .com / .io / .tech."""
    base = re.sub(r"\s*\(.*?\)", "", DIVISION_SUFFIX.sub("", company))
    base = OPF_RE.sub(" ", base).strip(" .,-")
    if re.search(r"\.(ru|com|io|tech|рф)\b", base, re.I):
        return ["https://" + re.sub(r"\s+", "", base.lower()) + "/"]
    words = [w for w in re.split(r"[^\w-]+", low(base)) if w]
    if not words or len(words) > 3:
        return []
    lat = "".join("".join(TRANSLIT.get(c, c) for c in w) for w in words)
    lat = re.sub(r"[^a-z0-9-]", "", lat)
    if len(lat) < 3:
        return []
    return [f"https://{lat}.{tld}/" for tld in ("ru", "com", "io", "tech")]


def brand_on_page(company, text):
    """Сайт «наш», если на главной есть хоть одно значимое слово бренда (≥ 4 букв)."""
    words = [w for w in name_tokens(DIVISION_SUFFIX.sub("", company)) if len(w) >= 4]
    page = low(text).translate(TO_CYR)
    return bool(words) and any(w in page for w in words)


def find_party(cache, key, ident):
    res = cached(cache, "byid", ident, lambda: requests_post(
        key, "findById/party", {"query": ident, "count": 1}))
    s = (res or {}).get("suggestions") or []
    return s[0] if s else None


def step2b():
    import sqlite3
    from collections import Counter
    rows = json.loads((WORK / "step2.json").read_text(encoding="utf-8"))
    sites = {}
    for f in sorted(WORK.glob("sites_out_*.json")):
        for x in json.loads(f.read_text(encoding="utf-8")):
            sites[x["company"]] = x
    key = dadata_key()
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    scache = json.loads(SITE_CACHE.read_text(encoding="utf-8")) if SITE_CACHE.exists() else {}
    db = sqlite3.connect(FNS_DB)
    stat = Counter()
    todo = [r for r in rows if r.get("Уверенность сопоставления") == "ручная"]
    for i, r in enumerate(todo, 1):
        x = sites.get(r["Компания"]) or {"company": r["Компания"], "note": "лимит"}
        unsearched = not x.get("site") and re.search(r"лимит", x.get("note") or "")
        if unsearched:                             # поиск не дошёл — пробуем угадать домен
            for url in guess_sites(r["Компания"]):
                text = fetch_site(scache, url)
                if not text.startswith("#") and brand_on_page(r["Компания"], text):
                    x = dict(x, site=url, guessed=True)
                    break
            if not x.get("site"):
                r["Причина ручной проверки"] = "сайт не искали (лимит поиска), домен не угадан"
                stat["не искали, домен не угадан"] += 1
                continue
            stat["домен угадан"] += 1
        if x.get("site"):
            r["Сайт"] = x["site"]
        ident, foreign, captcha = None, bool(x.get("foreign")), False
        if x.get("inn") and inn_ok(str(x["inn"])):
            ident, src = str(x["inn"]), "поиск"
        elif x.get("site"):
            ident, f2, captcha = site_inn(scache, x["site"])
            foreign = foreign or (f2 and not ident)
            src = "сайт (домен угадан)" if x.get("guessed") else "сайт"
        if captcha:
            stat["капча на сайте"] += 1
            r["Пометка"] = "капча на сайте, ИНН не проверен"
            continue
        party = find_party(cache, key, ident) if ident else None
        if party and (party["data"].get("state") or {}).get("status") not in DEAD:
            d = party["data"]
            staff, income = fns_row(db, d.get("inn"))
            code = d.get("okved") or ""
            r.update({
                "ИНН": d.get("inn"), "Юрлицо": party["value"], "ОКВЭД код": code,
                "ОКВЭД расшифровка": okved_name(cache, key, code), "Регион": region(d),
                "Статус": "действующая", "Численность": staff, "Доходы (ФНС)": income,
                "Уверенность сопоставления": "сайт", "Причина ручной проверки": "",
                "Кандидаты (топ-3)": "", "Источник ИНН": src,
                "Пометка": "нет данных ФНС по юрлицу" if staff is None else
                           "мало сотрудников в юрлице, возможно, штат в другом юрлице группы"
                           if staff < 10 else ""})
            stat[f"ИНН найден ({src})"] += 1
        elif party:
            stat["ИНН с сайта — ликвидировано"] += 1
        elif foreign or not x.get("site"):
            r["Регион"] = NOT_RF
            r["Причина ручной проверки"] = ("юрлицо не РФ" if foreign else "сайт не найден")
            stat["не РФ / нет юрлица РФ" + (" (нет сайта)" if not x.get("site") else "")] += 1
        else:
            stat["сайт есть, ИНН на нём не найден"] += 1
        if i % 25 == 0:
            print(f"  … {i}/{len(todo)}", flush=True)
    (WORK / "step2b.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1, default=str),
                                     encoding="utf-8")
    print(f"\nстрок на поиск по сайтам: {len(todo)}")
    for k, v in stat.most_common():
        print(f"  {v:4d}  {k}")
    print(f"результат: {WORK / 'step2b.json'}")


# ---------- шаги 3–4: скоринг и оффер ----------
TODAY = "2026-10-13"
LEAD_RE = rx(r"\bhead\b", r"\blead\b", r"руковод", r"тимлид", r"team ?lead", r"tech ?lead",
             r"архитект", r"architect", r"единственн", r"первый devops", r"first devops")
JUNIOR_RE = rx(r"junior", r"джун", r"младш", r"стаж", r"trainee", r"intern", r"ученик")
FOREIGN_NAME = re.compile(r"^\s*(ТОО|TOO|ЧК|ИП ООО)\b|\b(LLC|Ltd|GmbH|Georgia|Tashkent|Kazakhstan|"
                          r"Грузия|Ташкент|Казахстан|Узбекистан|Армения)\b", re.I)
OFFERS = [
    (1, "Chaos-тест отказоустойчивости", rx(r"\bsre\b", r"reliability", r"high availability",
                                             r"\bha\b", r"инцидент", r"надежн", r"надёжн")),
    (2, "Мониторинг и алертинг за 3 дня", rx(r"мониторинг", r"observability", r"prometheus",
                                              r"grafana", r"zabbix", r"\belk\b", r"логи",
                                              r"victoriametrics", r"opensearch", r"loki")),
    # сетевую и инфраструктурную безопасность не берём: это не про CI/CD
    (3, "Безопасность в CI/CD", rx(r"devsecops", r"appsec", r"application security",
                                    r"безопасн\w* разработ", r"\bsast\b", r"\bdast\b",
                                    r"gitleaks", r"trivy", r"sonarqube")),
    (4, "FinOps-аудит облаков", rx(r"\baws\b", r"\bgcp\b", r"azure", r"yandex ?cloud",
                                    r"яндекс ?облак", r"оптимизац\w* затрат", r"finops")),
    (5, "Пилотная миграция 1 сервиса", rx(r"миграц", r"переезд", r"импортозамещ", r"astra",
                                          r"астра", r"red ?os", r"ред ?ос")),
    (6, "Ansible для железа", rx(r"bare.?metal", r"сервер", r"виртуализ", r"vmware", r"proxmox",
                                  r"linux.?админ", r"системн\w* админ", r"ansible")),
    (7, "Единое dev-окружение на Docker", rx(r"помощь разработчик", r"рост команды", r"онбординг",
                                             r"docker", r"dev.?окружен")),
    (8, "Тест-стенды на каждый PR", rx(r"тестов\w* сред", r"\bstage\b", r"staging", r"qa.?окружен",
                                        r"стенд")),
    (9, "Helm-шаблонизация деплоя", rx(r"kubernetes", r"\bk8s\b", r"helm", r"openshift",
                                        r"deckhouse")),
    (10, "Ускорение CI/CD", rx(r"ci/cd", r"\bci\b", r"gitlab", r"jenkins", r"teamcity",
                               r"релиз", r"release", r"1с devops", r"1c devops")),
]
MLOPS = rx(r"mlops", r"\bml\b")
WHO = ((100, "CTO / основатель"), (500, "CTO / техдир"),
       (1000, "руководитель инфраструктуры/платформы или CIO"))


def as_date(v):
    return str(v)[:10] if v else ""


def offer(titles, stack):
    """(главный, запасной, зацепка, есть ли явный триггер)."""
    hits = []
    for num, name, pat in OFFERS:
        t_hits = [t for t in titles if pat.search(t)]
        s_hits = [s.strip() for s in (stack or "").split(",") if pat.search(s)]
        score = 2 * len(t_hits) + len(s_hits)
        if score:
            hits.append((score, -num, num, name, t_hits[0] if t_hits else s_hits[0]))
    hits.sort(reverse=True)
    if not hits and any(MLOPS.search(t) for t in titles):
        return "9. Helm-шаблонизация деплоя", "10. Ускорение CI/CD", titles[0], True
    if not hits:
        return ("10. Ускорение CI/CD", "2. Мониторинг и алертинг за 3 дня",
                titles[0] if titles else "", False)
    main = f"{hits[0][2]}. {hits[0][3]}"
    spare = f"{hits[1][2]}. {hits[1][3]}" if len(hits) > 1 else (
        "10. Ускорение CI/CD" if hits[0][2] != 10 else "2. Мониторинг и алертинг за 3 дня")
    return main, spare, hits[0][4], True


def score(r):
    """Баллы шага 3. Размерные пункты при неизвестной численности — 0 (правило 13.10.2026).
    Возвращает (балл, балл без размера, «почему»)."""
    from datetime import date
    titles = r.get("_все названия") or []
    staff = r.get("Численность")
    pts, why, size_pts = 0, [], 0
    old = as_date(r.get("Самая старая открытая"))
    if old and (date.fromisoformat(TODAY) - date.fromisoformat(old)).days >= 21:
        pts += 3
        why.append("+3 висит 21+ дн")
    if any(LEAD_RE.search(t) for t in titles):
        pts += 3
        why.append("+3 лид/первый DevOps")
    if (r.get("Вакансий инфраструктуры") or 0) >= 2 and staff is not None and staff <= 1000:
        size_pts += 2
        why.append("+2 2+ вакансии при ≤1000")
    if staff is not None and 50 <= staff <= 1000:
        size_pts += 2
        why.append("+2 численность 50–1000")
    if (r.get("Зарплата до") or 0) >= 300000:
        pts += 1
        why.append("+1 вилка до ≥300к")
    if r.get("_триггер"):
        pts += 1
        why.append("+1 триггер под оффер")
    if titles and all(JUNIOR_RE.search(t) for t in titles):
        pts -= 3
        why.append("−3 только junior")
    if r.get("Регион") == NOT_RF or FOREIGN_NAME.search(r["Компания"]):
        pts -= 2
        why.append("−2 не РФ")
    if (r.get("ОКВЭД код") or "").startswith("64.19") and (staff or 0) > 1000:
        pts -= 2
        why.append("−2 банк >1000")
    return pts + size_pts, pts, "; ".join(why) or "0"


def post_filter(r):
    """Дофильтр после обогащения: ОКВЭД и численность."""
    code, staff = r.get("ОКВЭД код") or "", r.get("Численность")
    if r["Исключение"]:
        return r["Исключение"]
    if code.startswith("78"):
        return "посредник"
    if code.startswith("84"):
        return "гос"
    if staff is not None and staff > 3000:
        return "гигант"
    if code.startswith("70.22"):
        return "посредник?"
    return ""


def step3():
    from collections import Counter
    src = WORK / "step2b.json"
    if not src.exists():                           # шаг 2.1 ещё не прогнан
        src = WORK / "step2.json"
    rows = json.loads(src.read_text(encoding="utf-8"))
    for r in rows:
        r["Исключение"] = post_filter(r)
        if r["Исключение"] and r["Исключение"] != "посредник?":
            r["Волна"] = ""
            r["Лист"] = "Исключены"
            continue
        titles = r.get("_все названия") or []
        r["Оффер"], r["Запасной оффер"], r["Зацепка"], r["_триггер"] = offer(titles, r.get("Стек"))
        r["Балл"], r["Балл без размера"], r["Почему"] = score(r)
        staff = r.get("Численность")
        r["Кого искать"] = "CTO" if staff is None else next(
            (w for lim, w in WHO if staff < lim), "руководитель инфраструктуры/платформы или CIO")
        notes = [r.get("Пометка")] if r.get("Пометка") else []
        if staff is None:
            if r["Балл без размера"] >= 5:
                r["Волна"], r["Лист"] = "", "Ручная проверка"
                notes.append("размер не определён, балл без размера ≥ 5")
            else:
                r["Волна"], r["Лист"] = 3, "Волна 3"
                notes.append("размер не определён")
        elif staff < 50:
            r["Волна"], r["Лист"] = 3, "Волна 3"
            notes.append("микро/малая")
        else:
            r["Волна"] = 1 if r["Балл"] >= 7 else 2 if r["Балл"] >= 4 else 3
            r["Лист"] = f"Волна {r['Волна']}"
        if r["Исключение"] == "посредник?":
            r["Лист"] = "Ручная проверка"
            notes.append("посредник?")
        r["Пометка"] = "; ".join(dict.fromkeys(notes))
    (WORK / "step3.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1, default=str),
                                    encoding="utf-8")

    sheets = Counter(r["Лист"] for r in rows)
    print("по листам:")
    for k in ("Волна 1", "Волна 2", "Волна 3", "Ручная проверка", "Исключены"):
        print(f"  {k:16s} {sheets.get(k, 0)}")
    conf = Counter(r.get("Уверенность сопоставления") for r in rows
                   if r.get("Уверенность сопоставления"))
    print("уверенность сопоставления:", dict(conf))
    manual = [r for r in rows if r["Лист"] == "Ручная проверка"]
    print("ручная проверка: размер неизвестен и балл без размера ≥ 5 —",
          sum("размер не определён" in r["Пометка"] for r in manual),
          "| посредник? —", sum("посредник?" in r["Пометка"] for r in manual))
    w1 = sorted((r for r in rows if r["Лист"] == "Волна 1"),
                key=lambda r: (-r["Балл"], as_date(r.get("Самая старая открытая"))))
    print("\nтоп-30 волны 1:")
    for r in w1[:30]:
        print(f"  {r['Балл']:2d} | {r['Компания'][:30]:30s} | {r['Численность'] or '—':>5} чел. | "
              f"{r['Оффер'][:34]:34s} | {r['Зацепка'][:50]}")
    print(f"результат: {WORK / 'step3.json'}")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["step1"]:
        step1()
    elif a[:1] == ["step2"]:
        step2(int(a[a.index("--limit") + 1]) if "--limit" in a else None)
    elif a[:1] == ["step2b"]:
        step2b()
    elif a[:1] == ["step3"]:
        step3()
    else:
        print(__doc__)
