"""Счётчики откликов по статусам и непрочитанные чаты на трёх аккаунтах.

Ничего не пишет и не отправляет. Открывает профиль, если он закрыт, и не
закрывает его, а оставляет одну пустую вкладку: у BitBrowser суточный
лимит на открытия профилей. Робот-рекрутер и автоответы считаются отдельно от
живых сообщений, но в «живые» всё равно может попасть анкета робота.
"""
import re, requests, pathlib
from tidy_tabs import tidy
from playwright.sync_api import sync_playwright

API = "http://127.0.0.1:54345"
ACCS = [("первый", "hh_autoreply"), ("второй", "hh_autoreply_2"), ("третий", "hh_autoreply_3")]
LABELS = ["Все", "Приглашение", "Собеседование", "Выход на работу", "Ожидание", "Отказ"]
MINE = "Отклик на вакансию"
AUTO = re.compile(r"^(Отказ|.{0,60}(Благодарим|Спасибо за (отклик|интерес|доверие))|.{0,40}Рассмотрим ваше резюме)", re.I)
ports = requests.post(f"{API}/browser/ports", json={}).json()["data"]


def pid_of(d):
    env = pathlib.Path(f"/Users/mr_macbook/Documents/{d}/.env").read_text()
    return re.search(r"^PROFILE_ID=(\S+)", env, re.M).group(1)


with sync_playwright() as p:
    for name, d in ACCS:
        pid = pid_of(d)
        opened = False
        if pid in ports:
            url = f"http://127.0.0.1:{ports[pid]}"
        else:
            r = requests.post(f"{API}/browser/open", json={"id": pid}, timeout=90).json()
            url = r["data"]["ws"]
            opened = True
        print(f"\n=== {name} аккаунт ===")
        try:
            ctx = p.chromium.connect_over_cdp(url).contexts[0]
            page = ctx.new_page()
            page.set_default_navigation_timeout(90000)
            page.goto("https://hh.ru/applicant/negotiations", wait_until="domcontentloaded")
            page.wait_for_timeout(6000)
            lines = [l.strip() for l in page.inner_text("body").split("\n")]
            got = {}
            for i, l in enumerate(lines):
                if l in LABELS and l not in got:
                    nxt = lines[i + 1] if i + 1 < len(lines) else ""
                    got[l] = int(re.sub(r"\D", "", nxt)) if re.fullmatch(r"[\d\s ]+", nxt or "x") else 0
            print("  " + ", ".join(f"{k} {v}" for k, v in got.items()))

            page.goto("https://hh.ru/chat", wait_until="domcontentloaded")
            page.wait_for_timeout(6000)
            f = page.get_by_text("Только непрочитанные").first
            if f.count():
                f.click()
                page.wait_for_timeout(4000)
            ls = [l.strip() for l in page.inner_text("body").split("\n") if l.strip()]
            rows, i = [], 0
            while i < len(ls) - 3:
                if re.fullmatch(r"\d{1,2}:\d{2}|вчера|\d{1,2} \w+|\d{2}\.\d{2}\.\d{4}", ls[i + 1]):
                    rows.append((ls[i], ls[i + 1], ls[i + 2], ls[i + 3]))
                    i += 4
                else:
                    i += 1
            norm = lambda s: re.sub(r"\s+", " ", s.replace("\xa0", " ")).strip()
            live = [r for r in rows
                    if MINE.lower() not in norm(r[3]).lower() and not AUTO.match(norm(r[3]))]
            print(f"  непрочитанных: {len(rows)}, живых: {len(live)}, "
                  f"автоответов/отказов: {len(rows) - len(live)}")
            for t, tm, comp, msg in live[:15]:
                print(f"   {tm:>6}  {comp[:30]:<32} {t[:34]:<36} → {msg[:80]}")
            page.close()
        except Exception as e:
            print("  ошибка:", type(e).__name__, str(e)[:80])
        if opened:
            tidy(pid)
