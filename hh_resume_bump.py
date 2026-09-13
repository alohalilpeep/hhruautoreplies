"""
Подъём резюме в поиске hh.ru через профиль BitBrowser.

hh разрешает бесплатный подъём раз в 4 часа. Карточка «Поднимите резюме
в поиске» есть на главной только когда подъём доступен, поэтому её отсутствие
это не ошибка, а «ещё рано».

Запуск:
    venv/bin/python hh_resume_bump.py          боевой
    venv/bin/python hh_resume_bump.py --dry    посмотреть, но не кликать

Настройки берутся из того же .env, что и hh_autoapply.py.
"""
import datetime as dt
import json
import sys
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

import hh_autoapply as hh

BUMP_CARD = '[data-qa="applicant-index-nba-action_update-resumes"]'
LOGIN_LINK = '[data-qa="login"]'
LOG_FILE = Path(__file__).with_name("bump.log")
STATE_FILE = Path(__file__).with_name("bump_state.json")

# hh отсчитывает 4 часа от момента подъёма. Запас, чтобы не ломиться на секунду
# раньше и не терять из-за этого целый цикл.
COOLDOWN = dt.timedelta(hours=4, minutes=2)

DRY = "--dry" in sys.argv
FORCE = "--force" in sys.argv      # игнорировать кулдаун, лезть в браузер


def read_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_state(**kw):
    state = read_state()
    state.update(kw)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                          encoding="utf-8")


def cooldown_left():
    """Сколько ещё ждать до следующего подъёма. None = можно прямо сейчас."""
    last = read_state().get("last_bump")
    if not last:
        return None
    try:
        left = dt.datetime.fromisoformat(last) + COOLDOWN - dt.datetime.now()
    except ValueError:
        return None
    return left if left > dt.timedelta(0) else None


def log(msg):
    line = f"{dt.datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def already_open():
    """Профиль уже открыт? Тогда его нельзя закрывать в конце: скорее всего
    за ним прямо сейчас сидит человек."""
    try:
        r = requests.post(f"{hh.BIT_API}/browser/ports", json={},
                          headers=hh._headers(), timeout=10).json()
        return hh.PROFILE_ID in (r.get("data") or {})
    except Exception:
        return False


def bump():
    if not hh.PROFILE_ID:
        log("ОШИБКА: PROFILE_ID не задан, заполни .env")
        return 2

    # Кулдаун ещё идёт: браузер не поднимаем и в лог не пишем, иначе при частом
    # расписании лог заплывёт пустыми строчками.
    left = cooldown_left()
    if left and not FORCE:
        mins = int(left.total_seconds() // 60)
        print(f"кулдаун, следующий подъём через ~{mins // 60} ч {mins % 60} мин")
        return 0

    was_open = already_open()

    try:
        ws = hh.open_profile()
    except Exception as e:
        log(f"ОШИБКА: BitBrowser не отдал профиль ({e})")
        return 2

    page = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(ws)
            ctx = browser.contexts[0]
            # своя вкладка: чужие могут висеть с прошлых запусков
            page = ctx.new_page()
            page.set_default_navigation_timeout(60000)

            # wait_until="commit": главная hh держит долгие фоновые запросы,
            # до domcontentloaded может не дойти в пределах таймаута
            page.goto("https://hh.ru/", wait_until="commit")
            page.wait_for_timeout(7000)

            if page.locator(LOGIN_LINK).count():
                log("ОШИБКА: профиль не залогинен на hh.ru")
                return 2

            card = page.locator(BUMP_CARD)
            if card.count() == 0:
                # hh считает, что рано. Подождём ещё цикл, состояние не трогаем.
                log("подъём недоступен, карточки нет")
                return 0

            if DRY:
                text = card.first.inner_text().strip().replace("\n", " ")
                log(f"--dry: карточка найдена ({text!r}), кликать не буду")
                return 0

            card.first.click()
            hh.pause(3, 5)

            # карточка пропадает после успешного подъёма
            if page.locator(BUMP_CARD).count() == 0:
                write_state(last_bump=dt.datetime.now().isoformat(timespec="seconds"))
                log("резюме поднято")
                return 0

            log("кликнули, но карточка на месте, проверь вручную")
            return 1
    except Exception as e:
        log(f"ОШИБКА: {type(e).__name__}: {str(e)[:200]}")
        return 2
    finally:
        if page:
            try:
                page.close()
            except Exception:
                pass
        if was_open:
            log("профиль был открыт до запуска, оставляю открытым")
        else:
            hh.close_profile()


if __name__ == "__main__":
    sys.exit(bump())
