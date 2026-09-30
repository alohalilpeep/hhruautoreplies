"""
Канарейка разметки: проверить, что hh не переехал.

Ничего не отправляет и никуда не кликает — только открывает страницу выдачи
и смотрит, на месте ли всё, из чего собран прогон. Поэтому гонять можно хоть
перед каждым запуском.

Ловит самый частый класс поломок. Уже случившиеся:
  • селектор карточек по префиксу data-qa находил 369 «карточек» вместо 50,
    потому что под него попадали вложенные элементы;
  • в поле зарплаты уезжало «Выплаты: два раза в месяц» — периодичность
    лежит под тем же корнем compensation;
  • кнопка письма в выдаче называется не так, как на странице вакансии.

    venv/bin/python tests/canary.py             по своему запросу
    venv/bin/python tests/canary.py --foryou    по подборке «Для вас»
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hh_searchapply as sa
import hh_autoapply as hh
from playwright.sync_api import sync_playwright

ART = Path(__file__).resolve().parent / "artifacts"
# на странице выдачи hh показывает пятьдесят вакансий
EXPECTED = 50
# «Выплаты: раз в месяц» — это периодичность, а не зарплата
NOT_SALARY = re.compile(r"выплат|раз в месяц|два раза", re.I)

problems = []
notes = []


def check(ok, good, bad):
    (notes if ok else problems).append(good if ok else bad)
    print(("  ок    " if ok else "  ПЛОХО ") + (good if ok else bad))


def main():
    url = None
    db = hh.init_db()
    ws = hh.open_profile()
    try:
        with sync_playwright() as p:
            ctx = p.chromium.connect_over_cdp(ws).contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_default_navigation_timeout(hh.NAV_TIMEOUT)
            if "--foryou" in sys.argv:
                url = sa.foryou_url(page)
                if not url:
                    print("не нашёл подборку «Для вас» на главной")
                    return 1
            url = url or hh.SEARCH_URL
            page.goto(url, wait_until="domcontentloaded")
            hh.pause(3, 5)

            if page.locator(hh.SEL["login_link"]).count():
                print("профиль не залогинен — проверять нечего")
                return 1
            if hh.captcha_present(page):
                print("на странице капча — проверять нечего")
                return 1

            print(f"страница: {page.url}\n")

            links = page.locator(hh.SEL["serp_link"]).count()
            cards = sa.cards_on_page(page)
            n = cards.count()
            check(n == links,
                  f"карточек {n}, ссылок на вакансии {links} — сходится",
                  f"карточек {n}, а ссылок {links}: селектор карточки ловит лишнее")
            check(0 < n <= EXPECTED + 5,
                  f"карточек на странице {n}",
                  f"карточек {n}, ожидали около {EXPECTED}")
            if not n:
                return 1

            buttons = page.locator(sa.RESPONSE_IN_CARD).count()
            check(buttons >= n - 5,
                  f"кнопок отклика {buttons} на {n} карточек",
                  f"кнопок отклика всего {buttons} на {n} карточек")

            # разбор карточек: по каждому полю считаем, у скольких оно нашлось
            got = {k: 0 for k in ("id", "title", "company", "salary",
                                  "experience", "address")}
            bad_salary = []
            for i in range(n):
                vid, info = sa.card_info(cards.nth(i))
                if vid:
                    got["id"] += 1
                for k in ("title", "company", "salary", "experience", "address"):
                    if (info.get(k) or "").strip():
                        got[k] += 1
                s = info.get("salary") or ""
                if s and NOT_SALARY.search(s):
                    bad_salary.append(s)

            check(got["id"] == n, f"id вакансии читается у всех {n}",
                  f"id не прочитался у {n - got['id']} карточек")
            check(got["title"] == n, f"название читается у всех {n}",
                  f"название не прочиталось у {n - got['title']} карточек")
            check(got["company"] >= n - 2,
                  f"компания читается у {got['company']} из {n}",
                  f"компания читается только у {got['company']} из {n}")
            check(not bad_salary,
                  "в зарплату не попадает периодичность выплат",
                  f"в зарплату попало: {bad_salary[:3]}")
            print(f"  (опыт у {got['experience']}, город у {got['address']} "
                  f"из {n} — поля необязательные)")

            # ключевые селекторы, на которых держится отклик
            for name, sel in (("кнопка отклика в карточке", sa.RESPONSE_IN_CARD),
                              ("ссылка на вакансию", hh.SEL["serp_link"])):
                check(page.locator(sel).count() > 0,
                      f"{name} находится",
                      f"{name} НЕ находится: {sel}")

            if problems:
                ART.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(ART / "canary.png"))
                print(f"\nснимок страницы: tests/artifacts/canary.png")
    finally:
        hh.close_profile()

    print(f"\nпроверок пройдено: {len(notes)}, провалено: {len(problems)}")
    if problems:
        print("hh, похоже, поменял разметку — прогон будет терять отклики:")
        for x in problems:
            print("  •", x)
        return 1
    print("разметка на месте, можно запускать прогон")
    return 0


if __name__ == "__main__":
    sys.exit(main())
