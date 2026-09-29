"""
Живая проверка отклика на зарубежную вакансию.

Модульные тесты в test_relocation.py проверяют нашу логику на заглушке.
Здесь наоборот: настоящий браузер, настоящая вакансия в другой стране —
чтобы увидеть, что hh вообще показывает и совпадает ли это с тем, что мы
ожидаем нажать.

ВНИМАНИЕ: тест отправляет НАСТОЯЩИЙ отклик, ровно один.

    venv/bin/python tests/e2e_relocation.py            один отклик
    venv/bin/python tests/e2e_relocation.py --dry      только дойти до кнопки

Пишет снимки экрана в tests/artifacts/ — до клика, после клика и в конце.
"""
import sys
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hh_searchapply as sa
import hh_autoapply as hh
from playwright.sync_api import sync_playwright

ART = Path(__file__).resolve().parent / "artifacts"
DRY = "--dry" in sys.argv

# Беларусь, Казахстан, Узбекистан, Грузия, Армения — области hh вне России
FOREIGN = ("https://hh.ru/search/vacancy?text=DevOps+OR+SRE&search_field=name"
           "&area=16&area=40&area=97&area=28&area=74")

WARN_WORDS = ("другой стране", "другом городе", "другая страна",
              "вакансия в другой", "переезд")


def shot(page, name):
    ART.mkdir(exist_ok=True)
    p = ART / f"{dt.datetime.now():%H%M%S}_{name}.png"
    try:
        page.screenshot(path=str(p))
        return p.name
    except Exception as e:
        return f"снимок не вышел: {e}"


def body(page, limit=200000):
    try:
        return page.inner_text("body")[:limit]
    except Exception:
        return ""


def main():
    db = hh.init_db()
    done = {r[0] for r in db.execute(
        "SELECT id FROM responses WHERE status NOT IN "
        "('error','dry_run','unknown','no_reaction')")}
    ws = hh.open_profile()
    facts = {}
    try:
        with sync_playwright() as p:
            ctx = p.chromium.connect_over_cdp(ws).contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_default_navigation_timeout(hh.NAV_TIMEOUT)
            page.goto(FOREIGN, wait_until="domcontentloaded")
            hh.pause(3, 5)

            cards = sa.cards_on_page(page)
            total = cards.count()
            print(f"зарубежных вакансий на странице: {total}")
            if not total:
                print("ВЕРДИКТ: не нашёл ни одной зарубежной вакансии")
                return 1

            target = None
            for i in range(total):
                vid, info = sa.card_info(cards.nth(i))
                if vid and vid not in done:
                    target = (vid, info)
                    break
            if not target:
                print("ВЕРДИКТ: все зарубежные вакансии уже отработаны")
                return 1

            vid, info = target
            print(f"берём: {info.get('title')} | {info.get('company')} "
                  f"| {info.get('address')} | id {vid}")

            card = page.locator(f'{sa.CARD}:has(a[href*="/vacancy/{vid}"])').first
            btn = card.locator(sa.RESPONSE_IN_CARD).first
            facts["кнопка отклика в карточке"] = bool(btn.count())
            if not btn.count():
                print("ВЕРДИКТ: в карточке нет кнопки отклика")
                return 1
            btn.scroll_into_view_if_needed()
            print("снимок до клика:", shot(page, "1_before"))
            if DRY:
                print("--dry: до кнопки дошли, не жму")
                return 0

            pages_before = len(page.context.pages)
            reacted = hh.click_apply(page, btn, pages_before)
            facts["клик дал реакцию"] = reacted
            hh.pause(1, 2)
            print("снимок после клика:", shot(page, "2_after_click"))

            txt = body(page)
            facts["hh предупредил о другой стране"] = any(
                w in txt.lower() for w in WARN_WORDS)
            facts["кнопка подтверждения по data-qa"] = bool(
                page.locator(hh.SEL["relocation_confirm"]).count())

            confirmed = sa.confirm_relocation(page)
            facts["подтверждение нажато"] = confirmed
            hh.pause(1, 2)
            print("снимок после подтверждения:", shot(page, "3_after_confirm"))

            status, letter_sent = sa.handle_popup(page, hh.render_letter(
                hh.clean_title(info.get("title", "")), info.get("company", "")))
            facts["попап отклика разобран"] = status is None
            facts["письмо отправлено"] = letter_sent

            final = "unknown"
            for attempt in range(4):
                if (card.get_by_text(hh.DONE_RE).count()
                        or page.get_by_text(hh.DONE_RE).count()
                        or card.locator(sa.ALREADY_IN_CARD).count()):
                    final = "applied"
                    break
                if "vacancy_response" in page.url:
                    final = "questions"
                    break
                page.wait_for_timeout(2500)
            facts["итоговый статус"] = final
            print("снимок в конце:", shot(page, "4_final"))
            hh.save(db, vid, f"https://hh.ru/vacancy/{vid}",
                    info.get("title", ""), info.get("company", ""),
                    final, letter_sent)
    finally:
        hh.close_profile()

    print("\n--- что произошло ---")
    for k, v in facts.items():
        print(f"  {k:36s} {v}")

    warned = facts.get("hh предупредил о другой стране")
    ok = facts.get("итоговый статус") in ("applied", "questions")
    print("\nВЕРДИКТ:")
    if warned and not facts.get("подтверждение нажато"):
        print("  ПЛОХО: hh просил подтвердить, а мы не нажали")
        return 1
    if warned and ok:
        print("  ХОРОШО: предупреждение было, подтвердили, отклик прошёл")
        return 0
    if not warned and ok:
        print("  отклик прошёл без предупреждения — "
              "на этой вакансии hh его не показал")
        return 0
    print(f"  ПЛОХО: отклик не подтвердился, статус "
          f"{facts.get('итоговый статус')}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
