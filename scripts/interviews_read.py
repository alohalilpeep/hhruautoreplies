"""Чаты вкладки «Собеседование» на трёх аккаунтах: хвост переписки по
каждому приглашению. Аргумент — сколько свежих приглашений читать.

Ничего не пишет. Последние строки чата вида «Какая схема оплаты?» — это
кнопки быстрых ответов hh, а не сообщения работодателя.
"""
import re, sys, requests, pathlib
from tidy_tabs import tidy
from playwright.sync_api import sync_playwright

API = "http://127.0.0.1:54345"
ACCS = [("первый", "hh_autoreply"), ("второй", "hh_autoreply_2"), ("третий", "hh_autoreply_3")]
URL = "https://hh.ru/applicant/negotiations?filter=all&state=INTERVIEW"
LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 8
ports = requests.post(f"{API}/browser/ports", json={}).json()["data"]


def pid_of(d):
    env = pathlib.Path(f"/Users/mr_macbook/Documents/{d}/.env").read_text()
    return re.search(r"^PROFILE_ID=(\S+)", env, re.M).group(1)


def norm(s):
    return re.sub(r"\s+", " ", s.replace("\xa0", " ")).strip()


def chat_text(page):
    """Текст открытого чата: он живёт во фрейме chatik или в панели на странице."""
    for fr in page.frames:
        if "chatik" in (fr.url or ""):
            try:
                return fr.locator("body").inner_text()
            except Exception:
                pass
    return page.inner_text("body")


with sync_playwright() as p:
    for name, d in ACCS:
        pid = pid_of(d)
        opened = pid not in ports
        url = (requests.post(f"{API}/browser/open", json={"id": pid}, timeout=90).json()["data"]["ws"]
               if opened else f"http://127.0.0.1:{ports[pid]}")
        print(f"\n################ {name} аккаунт ################")
        try:
            ctx = p.chromium.connect_over_cdp(url).contexts[0]
            page = ctx.new_page()
            page.set_default_navigation_timeout(90000)
            page.goto(URL, wait_until="domcontentloaded")
            page.wait_for_timeout(5000)
            items = page.locator('[data-qa="negotiations-item"]')
            n = items.count()
            print(f"приглашений на странице: {n}, читаю свежие {min(n, LIMIT)}")
            for i in range(min(n, LIMIT)):
                it = items.nth(i)
                vac = norm(it.locator('[data-qa="negotiations-item-vacancy"]').first.inner_text())
                comp = norm(it.locator('[data-qa="negotiations-item-company"]').first.inner_text())
                date = norm(it.locator('[data-qa="negotiations-item-date"]').first.inner_text())
                print(f"\n--- {date:>10} | {comp[:34]} | {vac[:60]}")
                try:
                    it.locator('[data-qa="open_chat"]').first.click(timeout=8000)
                    page.wait_for_timeout(5000)
                    lines = [norm(l) for l in chat_text(page).split("\n") if l.strip()]
                    tail = [l for l in lines if len(l) > 2][-12:]
                    for l in tail:
                        print("     ", l[:170])
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(1500)
                except Exception as e:
                    print("      чат не открылся:", type(e).__name__)
            page.close()
        except Exception as e:
            print("ошибка:", type(e).__name__, str(e)[:100])
        if opened:
            tidy(pid)
