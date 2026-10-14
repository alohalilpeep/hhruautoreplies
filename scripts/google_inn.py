"""ИНН компаний из выдачи Google — в браузере профиля BitBrowser, без API.

    python scripts/google_inn.py --limit 20        пробный прогон
    python scripts/google_inn.py                   все компании без ИНН с листа C

Запрос «<бренд> ИНН», смотрим первые 5 блоков выдачи. ИНН берём только из блока,
где есть слово бренда, — иначе цепляется соседний однофамилец. Найденный ИНН
проверяем контрольной суммой, DaData findById и fns.db. Пауза 15–30 с между
запросами; на капче ждём до 30 мин, пока её пройдут руками в окне браузера.
Кэш — exports/b2b_work/google_cache.json, повторный запуск продолжает с места.
"""
import json
import os
import random
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import quote_plus

sys.path.insert(0, str(Path(__file__).parent))
import b2b_score as b                              # noqa: E402

ACC = Path("/Users/mr_macbook/Documents/hh_autoreply_3")   # профиль hh3: у него есть окно
GCACHE = b.WORK / "google_cache.json"
INN_RE = re.compile(r"ИНН[\s:№]*(\d{12}|\d{10})(?!\d)")
CAPTCHA_RE = re.compile(r"необычный трафик|unusual traffic|не робот|not a robot", re.I)
# Сайты вакансий и отзывов: рядом с названием компании там ИНН самого агрегатора
# (пробный прогон: DatsTeam → «Эйч Карьера», Meteoro → «Дрим Джоб»). Такие блоки пропускаем.
AGG_RE = re.compile(r"dreamjob|dream job|hh\.ru|headhunter|superjob|career\.habr|хабр карьер|"
                    r"zarplata|rabota\.|работа\.ру|trudvsem|getmatch|avito|jobfilter|"
                    r"pravda-sotrudnikov|otrude|antijob|vakansii|вакансии в компании", re.I)
# Реестры юрлиц: ИНН оттуда принимаем, даже если название юрлица не похоже на бренд
REG_RE = re.compile(r"rusprofile|list-org|listorg|checko|zachestnyibiznes|за честный бизнес|"
                    r"sbis\.ru|сбис|audit-it|kontur|контур\.фокус|companies\.rbc|рбк компании|"
                    r"egrul|nalog\.ru|spark-interfax|vbankcenter|orginfo|ofdata|sravni\.ru/org", re.I)
LAT = re.compile(r"[a-z0-9]+")
# ИНН агрегаторов: выпали сразу у многих разных компаний в первом проходе
BAD_INN = {"9710095807", "333412951657", "9704271170"}
FOREIGN = re.compile(r"(?<![\w.])(ТОО|TOO|LLC|L\.L\.C|Ltd|LTD|GmbH|ЧК|Inc|B\.V\.|ОсОО|ИП ООО)(?![\w])"
                     r"|\.(kz|uz|kg|by|am|ge|az)\b", re.I)


def latin_tokens(s):
    """Слова названия латиницей: «АТИ.СУ» → {ati, su}, чтобы сравнить с брендом ATI.SU."""
    s = b.low(s)
    s = "".join(b.TRANSLIT.get(c, c) for c in s)
    return {t for t in LAT.findall(s) if len(t) >= 2}


def companies_without_inn():
    from openpyxl import load_workbook
    wb = load_workbook(b.OUT_V4, read_only=True)
    out = []
    for sheet in ("A", "B", "C"):
        rows = wb[sheet].iter_rows(values_only=True)
        h = next(rows)
        for row in rows:
            d = dict(zip(h, row))
            if not d["ИНН"]:
                out.append(d["Компания"])
    return out


def captcha(page):
    try:
        return "/sorry/" in page.url or bool(CAPTCHA_RE.search(page.locator("body").inner_text()[:3000]))
    except Exception:
        return False


def wait_captcha(page, limit=1800):
    print("\nGoogle показал капчу. Пройди её в окне браузера — жду до 30 мин.", flush=True)
    waited = 0
    while waited < limit:
        page.wait_for_timeout(10000)
        waited += 10
        if not captcha(page):
            print(f"капча пройдена за {waited} с, продолжаю", flush=True)
            return True
    print("капчу не прошли — останавливаюсь, кэш сохранён", flush=True)
    return False


def result_blocks(page):
    """Тексты первых 5 результатов выдачи (разметка Google меняется — два способа)."""
    for sel in ("#rso div.MjjYud", "#rso > div", "#search div.g"):
        loc = page.locator(sel)
        texts = [loc.nth(i).inner_text() for i in range(min(loc.count(), 8))]
        texts = [t for t in texts if t.strip()]
        if texts:
            return texts[:5]
    return []


def brand_words(company):
    q = b.queries(company)[0]
    return {w for w in b.name_tokens(q) if len(w) >= 3} or b.name_tokens(q)


def same_name(company, legal):
    words = brand_words(company)
    return bool(words & b.name_tokens(legal)) or bool(
        {w for w in latin_tokens(b.queries(company)[0]) if len(w) >= 3} & latin_tokens(legal))


def main():
    a = sys.argv[1:]
    arg = lambda k, d: type(d)(a[a.index(k) + 1]) if k in a else d  # noqa: E731
    limit, batch = arg("--limit", 0), arg("--batch", 50)
    pause_lo, pause_hi, rest = arg("--pause-min", 6), arg("--pause-max", 12), arg("--break", 120)
    names = companies_without_inn()
    gcache = json.loads(GCACHE.read_text(encoding="utf-8")) if GCACHE.exists() else {}
    # --retry: второй заход по тем, где ИНН не нашёлся, — другими формулировками и с ОГРН;
    # иностранцев (ТОО, LLC, ОсОО, домены .kz/.uz) не трогаем: российского ИНН у них нет
    retry = "--retry" in a
    suffixes = [" реквизиты ОГРН", " ООО юридическое лицо"] if retry else [" ИНН"]
    if retry:
        todo = [n for n in names if n in gcache and not gcache[n].get("inn")
                and not gcache[n].get("retried") and not FOREIGN.search(n)]
    else:
        todo = [n for n in names if n not in gcache]
    if limit:
        todo = todo[:limit]
    print(f"компаний без ИНН: {len(names)}, уже в кэше: {len(names) - len([n for n in names if n not in gcache])}, "
          f"в этот раз: {len(todo)}, партии по {batch}, пауза {pause_lo}–{pause_hi} с, "
          f"перерыв между партиями {rest} с", flush=True)

    key = b.dadata_key()
    dcache = json.loads(b.CACHE.read_text(encoding="utf-8")) if b.CACHE.exists() else {}
    fns = sqlite3.connect(b.FNS_DB)

    os.chdir(ACC)
    sys.path.insert(0, str(ACC))
    import hh_autoapply as hh
    from playwright.sync_api import sync_playwright
    ws = hh.open_profile()
    from collections import Counter
    total = Counter()
    try:
        with sync_playwright() as p:
            page = hh.fresh_page(p.chromium.connect_over_cdp(ws).contexts[0])
            stopped = False
            for start in range(0, len(todo), batch):
                part = todo[start:start + batch]
                stat = Counter()
                print(f"\n=== партия {start // batch + 1}: компании {start + 1}–{start + len(part)} "
                      f"из {len(todo)}", flush=True)
                for i, name in enumerate(part, start + 1):
                    q = b.queries(name)[0]
                    words = brand_words(name)
                    hits, used = {}, ""            # ИНН/ОГРН → {n: сколько блоков, reg: из реестра}
                    for suffix in suffixes:        # формулировки по очереди, до первой находки
                        page.goto("https://www.google.com/search?q="
                                  f"{quote_plus(q + suffix)}&hl=ru", wait_until="domcontentloaded")
                        page.wait_for_timeout(random.randint(1500, 3000))
                        if captcha(page) and not wait_captcha(page):
                            stopped = True
                            break
                        for t in result_blocks(page):
                            if AGG_RE.search(t):   # агрегатор вакансий/отзывов — ИНН чужой
                                continue
                            if not words & b.name_tokens(t) and not (
                                    latin_tokens(q) & latin_tokens(t)):
                                continue           # в блоке нет бренда — не наш ИНН
                            ids = [x for x in INN_RE.findall(t) if b.inn_ok(x)] + \
                                  [x for x in b.OGRN_RE.findall(t)] if retry else \
                                  [x for x in INN_RE.findall(t) if b.inn_ok(x)]
                            for x in ids:
                                if x in BAD_INN:
                                    continue
                                h = hits.setdefault(x, {"n": 0, "reg": False})
                                h["n"] += 1
                                h["reg"] = h["reg"] or bool(REG_RE.search(t))
                        if hits:
                            used = suffix
                            break
                        if len(suffixes) > 1:
                            page.wait_for_timeout(random.randint(pause_lo * 1000, pause_hi * 1000))
                    if stopped:
                        break
                    rec = {"query": q + used, "inns": hits, "inn": "", "legal": "", "match": "",
                           "retried": retry}
                    if hits:
                        inn = max(hits, key=lambda k: (hits[k]["reg"], hits[k]["n"]))
                        party = b.find_party(dcache, key, inn)
                        if party and (party["data"].get("state") or {}).get("status") not in b.DEAD:
                            d = party["data"]
                            staff, income = b.fns_row(fns, d.get("inn"))
                            match = ("название совпало" if same_name(name, party["value"])
                                     else "реестр" if hits[inn]["reg"] else "проверить")
                            rec.update({"inn": d.get("inn"), "legal": party["value"],
                                        "okved": d.get("okved") or "", "region": b.region(d),
                                        "staff": staff, "income": income, "match": match})
                    stat[rec["match"] or "не найден"] += 1
                    gcache[name] = rec
                    GCACHE.write_text(json.dumps(gcache, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
                    print(f"[{i}/{len(todo)}] {name[:34]:34s} → {rec['inn'] or '—':12s} "
                          f"{(rec['legal'] or '')[:34]:34s} {rec['match']:16s} "
                          f"{rec.get('staff') if rec['inn'] else ''}", flush=True)
                    if i < start + len(part):
                        page.wait_for_timeout(random.randint(pause_lo * 1000, pause_hi * 1000))
                total.update(stat)
                print(f"=== итог партии: {dict(stat)}", flush=True)
                if stopped:
                    break
                if start + batch < len(todo):
                    print(f"перерыв {rest} с", flush=True)
                    page.wait_for_timeout(rest * 1000)
    finally:
        hh.close_profile()
    print(f"\nвсего: {dict(total)}")


if __name__ == "__main__":
    main()
