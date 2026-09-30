"""
Отклики прямо из поисковой выдачи, без захода на карточку вакансии.

Зачем отдельный способ. Обычный прогон (hh_autoapply.py) открывает
hh.ru/vacancy/<id> по прямой ссылке — двести раз подряд, без единого
перехода из поиска. Человек так не ходит: он листает выдачу и жмёт
«Откликнуться» прямо в списке. На втором аккаунте прямые заходы, похоже,
и стали заметны — руками из интерфейса отклик проходит, скриптом нет.

Здесь мы повторяем человеческий путь: одна страница выдачи, клики по
кнопкам в карточках, следующая страница.

Факты о вакансии тут НЕ собираются. Их снимает первый аккаунт своим
обычным прогоном, и второй раз обходить те же страницы незачем.

Ветвления те же, что и в обычном прогоне, и упрощать их нельзя:
  already            — на вакансию уже откликались
  external           — отклик на сайте работодателя, новая вкладка
  questions          — анкета работодателя, отвечаем из банка
  skipped_question   — среди вопросов есть отброшенный вручную
  needs_letter       — письмо обязательно, а режим его запрещает
  resume_hidden      — резюме скрыто от работодателей
  no_button          — кнопки отклика в карточке нет
  no_reaction        — кнопка есть, но клик ничего не дал

    venv/bin/python hh_searchapply.py            прогон по своему запросу
    venv/bin/python hh_searchapply.py --foryou   подборка «Для вас» страницей поиска
    venv/bin/python hh_searchapply.py --main     то же, но прямо с главной страницы
    venv/bin/python hh_searchapply.py --pages 3  только первые 3 страницы

«Для вас» — это рекомендации самого hh по резюме и истории просмотров.
Источник отдельный от нашего запроса: туда попадает то, что под наши
фильтры по названию не подходит, а по сути подходит.
"""
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent))
import hh_autoapply as hh

# Карточка вакансии в выдаче. Селектор точный, а не по префиксу: внутри
# карточки десяток элементов с data-qa, начинающимся так же
# (vacancy-serp__vacancy-employer, -address, -work-experience…), и по
# префиксу на страницу из 50 вакансий находилось 369 «карточек».
CARD = '[data-qa="vacancy-serp__vacancy"]'
RESPONSE_IN_CARD = '[data-qa="vacancy-serp__vacancy_response"]'
ALREADY_IN_CARD = '[data-qa*="view-topic"]'

# Подтверждение отклика на вакансию в другом городе или стране. Подпись
# кнопки у hh разная, поэтому ищем по смыслу, а не по точному тексту.
RELOC_RE = re.compile(r"всё равно|все равно|подтвердить", re.I)

# «Отклик уже просмотрен работодателем» — письмо прикладывать поздно,
# hh его не примет, а форма остаётся висеть и держит прогон.
VIEWED_RE = re.compile(r"отклик уже просмотрен", re.I)
# «откликнуться» сюда класть нельзя: так подписаны кнопки в самих
# карточках, и подтверждение утащило бы клик в случайную вакансию.

# Что можно снять прямо из карточки, не открывая вакансию
IN_CARD = {
    "company": ('[data-qa="vacancy-serp__vacancy-employer"]',
                '[data-qa="vacancy-serp__vacancy-employer-text"]'),
    # frequency исключаем явно: у hh «Выплаты: два раза в месяц» лежит под
    # тем же корнем compensation и иначе уезжает в поле зарплаты
    "salary": ('[data-qa="vacancy-serp__vacancy-compensation"]',
               '[data-qa*="compensation"]:not([data-qa*="frequency"])'),
    "experience": ('[data-qa^="vacancy-serp__vacancy-work-experience"]',),
    "address": ('[data-qa="vacancy-serp__vacancy-address"]',),
    "work_format": ('[data-qa*="work-schedule"]', '[data-qa*="work-format"]'),
}


# Статусы, на которых что-то пошло не так и хочется увидеть страницу глазами.
# Снимок и текст ложатся в tests/artifacts/ — там же, где артефакты живого
# теста. Каталог в .gitignore, в репозиторий ничего не уедет.
SNAP_STATUSES = {"no_reaction", "unknown", "error", "no_button"}
ART = Path(__file__).resolve().parent / "tests" / "artifacts"


def snapshot(page, vid, status, title=""):
    """Сохранить, как выглядела страница в момент заминки.

    Без этого причина сбоя остаётся догадкой: в логе только статус, а что
    показывал hh — неизвестно. Так вскрылись и окно про другую страну,
    и попап, который не успевал открыться.
    """
    try:
        ART.mkdir(parents=True, exist_ok=True)
        stamp = hh.dt.datetime.now().strftime("%H%M%S")
        base = ART / f"{stamp}_{status}_{vid}"
        page.screenshot(path=str(base.with_suffix(".png")))
        body = ""
        try:
            body = page.inner_text("body")[:20000]
        except Exception:
            pass
        base.with_suffix(".txt").write_text(
            f"вакансия: {title}\nid: {vid}\nстатус: {status}\n"
            f"адрес: {page.url}\n\n{body}", encoding="utf-8")
    except Exception:
        pass            # диагностика не должна ронять прогон


def cards_on_page(page):
    """Карточки вакансий на странице выдачи.

    Основной селектор — по data-qa карточки. Если hh его снова переименует,
    поднимаемся от ссылки на вакансию к её карточке: ссылка есть всегда,
    иначе выдачу вообще нельзя было бы читать.
    """
    cards = page.locator(CARD)
    if cards.count():
        return cards
    return page.locator(
        f'{hh.SEL["serp_link"]} >> xpath=ancestor::*[@data-qa][1]')


def _first_text(card, selectors):
    """Текст по первому селектору, который что-то нашёл. У части полей hh
    держит два разных data-qa, и какой именно — зависит от вакансии."""
    for sel in selectors:
        el = card.locator(sel).first
        if el.count():
            t = (el.inner_text() or "").replace("\xa0", " ").strip()
            if t:
                return re.sub(r"\s+", " ", t)
    return ""


def card_info(card):
    """Всё, что карточка отдаёт без захода на вакансию."""
    link = card.locator(hh.SEL["serp_link"]).first
    if not link.count():
        return None, {}
    href = link.get_attribute("href") or ""
    m = re.search(r"/vacancy/(\d+)", href)
    info = {k: _first_text(card, sels) for k, sels in IN_CARD.items()}
    info["title"] = re.sub(r"\s+", " ", (link.inner_text() or "").strip())
    return (m.group(1) if m else None), info


def save_card_facts(db, vid, info):
    """Факты из карточки выдачи.

    Дописываем, а не перезаписываем: описание и ключевые навыки видны только
    на самой вакансии, их собирает подробный обход. Обычный INSERT OR REPLACE
    затёр бы их пустотой, а потом это уехало бы в общую аналитику при слиянии.
    """
    if db is None or not vid:
        return
    lo, hi, cur = hh.parse_salary(info.get("salary"))
    now = hh.dt.datetime.now().isoformat(timespec="seconds")
    db.execute(
        "INSERT INTO vacancy_facts (vacancy_id, salary_from, salary_to, "
        " currency, experience, work_format, scraped_ts, first_seen, status) "
        "VALUES (?,?,?,?,?,?,?,?,'active') "
        "ON CONFLICT(vacancy_id) DO UPDATE SET "
        "  salary_from = COALESCE(excluded.salary_from, salary_from), "
        "  salary_to   = COALESCE(excluded.salary_to,   salary_to), "
        "  currency    = COALESCE(excluded.currency,    currency), "
        "  experience  = COALESCE(experience,  excluded.experience), "
        "  work_format = COALESCE(work_format, excluded.work_format), "
        "  scraped_ts  = excluded.scraped_ts",
        (vid, lo, hi, cur, info.get("experience") or None,
         info.get("work_format") or info.get("address") or None, now, now))
    db.commit()


def confirm_relocation(page):
    """Подтвердить отклик на вакансию в другом городе или стране.

    Без подтверждения отклик не уходит, а на странице ничего не меняется —
    мы записывали такую вакансию как unknown и теряли её молча. После снятия
    фильтра по региону это стало всплывать на каждой второй зарубежной.

    Возвращает True, если подтверждение было нажато.
    """
    reloc = page.locator(hh.SEL["relocation_confirm"])
    if not reloc.count():
        # Запасной путь — ТОЛЬКО внутри всплывающего окна. На странице выдачи
        # полсотни кнопок «Откликнуться», и поиск по тексту без этой границы
        # уводил клик в случайную соседнюю вакансию.
        dialog = page.locator('[role="dialog"], [data-qa="bottom-sheet-content"]')
        if not dialog.count():
            return False
        reloc = dialog.get_by_role("button", name=RELOC_RE)
    if not reloc.count():
        return False
    try:
        reloc.first.click()
    except Exception:
        return False
    hh.pause(1, 2)
    return True


def close_leftover(page):
    """Закрыть окно, если после отправки оно осталось на экране.

    hh закрывает его сам не всегда: например, когда пишет «отклик уже
    просмотрен работодателем». Висящее окно перекрывает выдачу, и клик
    по следующей карточке уходит в него.
    """
    try:
        btn = page.locator(hh.SEL["popup_close"])
        if btn.count() and btn.first.is_visible():
            btn.first.click()
            hh.pause(1, 2)
            return True
        if hh.visible(page, hh.SEL["popup"]):
            page.keyboard.press("Escape")
            hh.pause(1, 2)
            return True
    except Exception:
        pass
    return False


def handle_popup(page, letter):
    """Разобрать попап отклика. Возвращает (статус, письмо_ушло).

    Статус None означает «попапа не было» — значит отклик ушёл мгновенно
    или открылась анкета, это разбирает вызывающий.
    """
    submit = page.locator(hh.SEL["popup_submit"])
    if not submit.count():
        return None, False

    # На этот отклик работодатель уже посмотрел: отправлять нечего,
    # а попап сам не закроется и застопорит прогон.
    if page.get_by_text(VIEWED_RE).count():
        print("    отклик уже просмотрен работодателем — закрываю")
        hh.close_popup(page)
        return "already", False

    if hh.RESUME_TITLE:
        scope = (page.locator(hh.SEL["popup"])
                 if page.locator(hh.SEL["popup"]).count() else page)
        found = scope.get_by_text(hh.RESUME_TITLE, exact=False)
        if found.count():
            found.first.click()
            hh.pause(0.5, 1.5)

    # резюме скрыто от работодателей: отклик не примут, ничего не жмём
    if hh.visible(page, hh.SEL["hidden_resume"]):
        hh.close_popup(page)
        return "resume_hidden", False

    need_letter = hh.letter_required(page)
    if need_letter and hh.LETTER_MODE == "never":
        hh.close_popup(page)
        return "needs_letter", False

    letter_sent = False
    if need_letter or hh.LETTER_MODE == "always":
        toggle = page.locator(hh.SEL["letter_toggle"])
        if toggle.count():
            toggle.first.click()
            hh.pause(0.5, 1.5)
        area = page.locator(hh.SEL["letter_input"])
        if area.count():
            area.first.fill(letter)
            letter_sent = True
            hh.pause(1, 2)

    submit.first.click()
    hh.pause(2, 4)
    if page.get_by_text(hh.LIMIT_RE).count():
        raise hh.LimitReached()
    # Отправили — закрываем окно крестиком. hh не всегда убирает его сам:
    # иногда вместо подтверждения показывает «отклик уже просмотрен
    # работодателем», и окно висит, загораживая следующую карточку.
    close_leftover(page)
    return None, letter_sent


def handle_questions(page, db, vid, url, company, letter):
    """Анкета работодателя. Та же логика, что и в обычном прогоне."""
    qs = hh.scrape_questions(page)
    if db is not None and qs:
        hh.save_questions(db, vid, url, company, qs)
        print(f"    собрано вопросов: {len(qs)}")

    dropped = hh.load_skipped_questions(db)
    if dropped and any(hh.normalize_question(q["question"]) in dropped
                       for q in qs):
        return "skipped_question"

    # Пустой ответ значит «нечем отвечать»: в банке нет одобренных ответов
    # на часть вопросов. Вакансию откладываем, как и в обычном прогоне.
    return hh.answer_questions(page, qs, hh.load_answer_bank(db), letter,
                               hh.load_choice_bank(db)) or "questions"


def apply_from_card(page, card, db, vid, title, company):
    """Отклик по кнопке в карточке выдачи. Возвращает статус."""
    if hh.is_blacklisted(company):
        return "blacklisted"
    if card.locator(ALREADY_IN_CARD).count():
        return "already"

    # Чужое окно, оставшееся от предыдущей вакансии, накрывает всю выдачу,
    # и клик уходит в него, а не в карточку. Так один медленно открывшийся
    # попап портил всю страницу: восемь промахов подряд из пятнадцати.
    if close_leftover(page):
        print("    закрыл окно от предыдущей вакансии")

    btn = card.locator(RESPONSE_IN_CARD).first
    if not btn.count() or not btn.is_visible():
        # Кнопки нет по двум разным причинам, и путать их нельзя: либо на
        # вакансию уже откликались, либо отклик идёт на сайте работодателя.
        # Первое hh показывает прямо в карточке надписью «вы откликнулись».
        if card.get_by_text(hh.DONE_RE).count():
            return "already"
        return "no_button"
    if hh.DRY_RUN:
        return "dry_run"
    if hh.captcha_present(page):
        raise hh.CaptchaFound()

    url = f"https://hh.ru/vacancy/{vid}"
    letter = hh.render_letter(hh.clean_title(title), company)
    pages_before = len(page.context.pages)
    search_url = page.url

    # На странице полсотни карточек, нижние за пределами экрана. Клик по
    # тому, чего не видно, Playwright считает ошибкой, а не промахом.
    try:
        btn.scroll_into_view_if_needed()
        hh.pause(0.5, 1.5)
    except Exception:
        pass

    if not hh.click_apply(page, btn, pages_before):
        if hh.captcha_present(page):
            raise hh.CaptchaFound()
        # Попап мог всплыть уже после того, как мы перестали ждать.
        # Не закроем — он накроет следующие карточки.
        close_leftover(page)
        return "no_reaction"

    # отклик на сайте работодателя: открылась новая вкладка
    if len(page.context.pages) > pages_before:
        for p in page.context.pages[pages_before:]:
            p.close()
        return "external"

    if page.get_by_text(hh.LIMIT_RE).count():
        raise hh.LimitReached()

    # «Вы откликаетесь на вакансию в другой стране (городе)» — hh просит
    # подтвердить. Раньше это всплывало редко, а после снятия фильтра по
    # региону стало на каждой второй зарубежной вакансии. Без подтверждения
    # отклик не уходит, и мы записывали unknown.
    confirm_relocation(page)

    # hh увёл со страницы выдачи на анкету
    if "vacancy_response" in page.url or page.locator(hh.SEL["task"]).count():
        status = handle_questions(page, db, vid, url, company, letter)
        page.goto(search_url, wait_until="domcontentloaded")
        hh.pause(2, 4)
        return status

    status, letter_sent = handle_popup(page, letter)
    if status:
        hh.LAST_LETTER_SENT = letter_sent
        return status

    # Отклик ушёл мгновенно, без попапа. Письмо работодатель не требовал,
    # но hh предлагает приложить его следом — ссылкой «Приложить письмо».
    # В обычном прогоне эта ветка есть, в выдаче я её сперва не перенёс,
    # и при мгновенном отклике письмо не уходило никогда.
    if hh.LETTER_MODE == "always" and not letter_sent:
        # Ссылку ищем ВНУТРИ своей карточки. На странице полсотни карточек,
        # и поиск по всей странице цеплял первую попавшуюся — письмо могло
        # уехать к чужой вакансии, а к своей не уйти вовсе.
        toggle = card.locator(hh.SEL["letter_after_toggle"]).first
        if toggle.count() and toggle.is_visible():
            toggle.click()
            hh.pause(1, 2)
            if (card.get_by_text(VIEWED_RE).count()
                    or page.get_by_text(VIEWED_RE).count()):
                print("    отклик уже просмотрен — письмо прикладывать поздно")
                hh.close_popup(page)
                hh.LAST_LETTER_SENT = False
                return "applied"
            area = card.locator(hh.SEL["letter_after_input"]).first
            if not area.count():                 # форма письма может всплыть
                area = page.locator(hh.SEL["letter_after_input"]).first
            if area.count() and area.is_visible():
                area.fill(letter)
                hh.pause(1, 2)
                submit = card.locator(hh.SEL["letter_after_submit"]).first
                if not submit.count():
                    submit = page.locator(hh.SEL["letter_after_submit"]).first
                if submit.count():
                    submit.click()
                    hh.pause(2, 3)
                    letter_sent = True
                    close_leftover(page)
    hh.LAST_LETTER_SENT = letter_sent

    # Подтверждение ищем в самой карточке: на странице выдачи «вы откликнулись»
    # появляется именно там, а не на всю страницу, как на карточке вакансии.
    for attempt in range(4):
        if (card.locator(ALREADY_IN_CARD).count()
                or card.get_by_text(hh.DONE_RE).count()
                or page.get_by_text(hh.DONE_RE).count()):
            return "applied"
        if attempt < 3:
            page.wait_for_timeout(2500)
    return "unknown"


def foryou_url(page):
    """Адрес подборки «Для вас» с главной страницы.

    Это обычный поиск, подобранный hh под резюме: /search/vacancy?resume=…
    Ссылку берём со страницы, а не прописываем руками: идентификатор резюме
    у каждого аккаунта свой и меняется при пересоздании резюме.
    """
    page.goto("https://hh.ru", wait_until="domcontentloaded")
    hh.pause(3, 5)
    link = page.locator('[data-qa*="presets"] a').filter(has_text="Для вас").first
    if not link.count():
        return None
    href = link.get_attribute("href") or ""
    return urljoin("https://hh.ru", href.split("&hhtmFrom")[0])


def run(page, db, pages, url=None, home=False):
    """home=True — работаем на самой главной, не уходя в поиск.

    hh показывает там всего шесть рекомендаций за раз, поэтому вместо
    страниц выдачи крутим круги: откликнулись на видимое, перезагрузили
    главную, hh подставил новые. Останавливаемся, когда три круга подряд
    не приносят ничего нового.
    """
    retry = ["error", "dry_run", "needs_letter", "resume_hidden",
             "unknown", "no_reaction"]
    if hh.AUTO_ANSWER:
        retry.append("questions")
    done = {r[0] for r in db.execute(
        f"SELECT id FROM responses WHERE status NOT IN "
        f"({','.join('?' * len(retry))})", retry)}

    url = url or hh.SEARCH_URL
    sep = "&" if "?" in url else "?"
    empty_rounds = 0
    seen = unknown_row = 0
    # hh между запросами перемешивает выдачу, и одна вакансия попадается
    # на разных страницах. Без этой памяти мы заходили на неё второй раз,
    # видели уже отправленный отклик — и писали его результат поверх
    # собственного «отправлено» получасовой давности.
    handled = set()
    for n in range(pages):
        if home:
            page.goto("https://hh.ru", wait_until="domcontentloaded")
        else:
            page.goto(f"{url}{sep}page={n}", wait_until="domcontentloaded")
        hh.pause(3, 6)
        cards = cards_on_page(page)
        total = cards.count()
        if not total:
            print(f"{'круг' if home else 'страница'} {n}: карточек нет, "
                  f"дальше не идём")
            break
        print(f"\n— {'круг' if home else 'страница'} {n}: карточек {total}")

        # Сначала переписываем всё, что на странице, и только потом кликаем.
        # После отклика hh перерисовывает выдачу: кнопка в карточке меняется
        # на «вы откликнулись». Ссылки на элементы по номеру в списке после
        # этого протухают, клик падает с ошибкой — и отклик записывался как
        # no_reaction, хотя кликнуть было просто некуда.
        page_cards = []
        for i in range(total):
            try:
                vid, info = card_info(cards.nth(i))
            except Exception:
                continue
            if vid:
                page_cards.append((vid, info))

        if home:
            fresh = [v for v, _ in page_cards if v not in done and v not in handled]
            empty_rounds = 0 if fresh else empty_rounds + 1
            if empty_rounds >= 3:
                print("три круга подряд без новых рекомендаций — хватит")
                break

        for vid, info in page_cards:
            if hh.applied_today(db) >= hh.DAILY_LIMIT:
                print("Свой дневной лимит достигнут")
                return
            if vid in done or vid in handled:
                continue
            handled.add(vid)
            # карточку ищем заново по ссылке на вакансию: этот поиск
            # переживает любую перерисовку страницы
            card = page.locator(f'{CARD}:has(a[href*="/vacancy/{vid}"])').first
            if not card.count():
                continue
            title = info.get("title", "")
            company = info.get("company", "")
            # Факты пишем до отклика и до ветвлений — тогда в выборку попадут
            # и те вакансии, на которые откликнуться не удалось.
            save_card_facts(db, vid, info)
            seen += 1
            hh.LAST_LETTER_SENT = False
            try:
                status = apply_from_card(page, card, db, vid, title, company)
            except hh.LimitReached:
                print("hh пишет, что лимит откликов исчерпан")
                return
            except hh.CaptchaFound:
                # Ждём человека, а не падаем. Вакансию не записываем —
                # вернётся в этот же прогон следующим кругом или в следующий.
                if hh.wait_captcha(page):
                    continue
                return
            except Exception as e:
                if "has been closed" in str(e) or "Target closed" in str(e):
                    print("\nБраузер закрылся. Останавливаюсь.")
                    return
                status = "error"
                print(f"Ошибка на вакансии {vid}: {e}")

            if status in SNAP_STATUSES:
                snapshot(page, vid, status, title)
            unknown_row = unknown_row + 1 if status == "unknown" else 0
            hh.save(db, vid, f"https://hh.ru/vacancy/{vid}", title, company,
                    status, hh.LAST_LETTER_SENT)
            mark = " +письмо" if hh.LAST_LETTER_SENT else ""
            money = f" | {info['salary']}" if info.get("salary") else ""
            print(f"[{status}{mark}] {title} | {company}{money}")
            if unknown_row >= hh.UNKNOWN_STREAK:
                print(f"\n{hh.UNKNOWN_STREAK} откликов подряд без подтверждения. "
                      f"Останавливаюсь, вакансии не потеряны.")
                return
            hh.pause(20, 60) if status in ("applied", "answered") else hh.pause(4, 10)

    print(f"\nпросмотрено новых карточек: {seen}")


def main():
    if not hh.PROFILE_ID:
        raise SystemExit("Не задан PROFILE_ID в .env")
    if not hh.SEARCH_URL and not {"--foryou", "--main"} & set(sys.argv):
        raise SystemExit("Не задан SEARCH_URL в .env")
    pages = hh.MAX_PAGES
    if "--pages" in sys.argv:
        i = sys.argv.index("--pages")
        if len(sys.argv) > i + 1:
            pages = int(sys.argv[i + 1])

    foryou = "--foryou" in sys.argv
    home = "--main" in sys.argv

    db = hh.init_db()
    ws = hh.open_profile()
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(ws)
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_default_navigation_timeout(hh.NAV_TIMEOUT)
            page.goto("https://hh.ru", wait_until="domcontentloaded")
            hh.pause(2, 3)
            if page.locator(hh.SEL["login_link"]).count():
                print("Профиль не залогинен на hh.ru, зайди руками и перезапусти")
                return
            url = None
            if home:
                print("откликаемся прямо с главной, из блока «Для вас»")
            elif foryou:
                url = foryou_url(page)
                if not url:
                    print("Не нашёл подборку «Для вас» на главной")
                    return
                print(f"подборка «Для вас»: {url}")
            run(page, db, pages, url, home=home)
            print(f"За сегодня откликов: {hh.applied_today(db)}")
    finally:
        hh.close_profile()


if __name__ == "__main__":
    main()
