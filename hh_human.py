"""
Человеческий ритм: паузы, прокрутка, перерывы.

Зачем отдельным модулем. Паузы были размазаны по коду случайными числами
в каждом месте, и поведение выходило ровным, как метроном: пауза после
отклика всегда 20–60 секунд, между карточками всегда 4–10, прокрутки нет
вовсе. Человек так не работает — он читает, отвлекается, листает туда-сюда
и иногда уходит за кофе.

Честная оговорка. По нашим же логам темп почти не влияет на то, покажет
ли hh капчу: на молодых аккаунтах она приходила через 7–13 откликов при
любых паузах, после смены адреса и на свежем профиле. Этот модуль делает
поведение естественнее и размазывает нагрузку, но капчу он не отменяет.

    import hh_human as human
    human.settle(page)        # осмотреться перед кликом
    human.after_apply()       # пауза после отклика
    human.between()           # пауза между карточками
"""
import os
import random
import time

# Базовые паузы. Задаются в .env, чтобы темп можно было менять, не трогая код.
APPLY_PAUSE = os.getenv("APPLY_PAUSE", "20-60")      # после отклика
STEP_PAUSE = os.getenv("STEP_PAUSE", "4-10")         # между карточками
# Раз в сколько-то действий человек отвлекается надолго
BREATHER_EVERY = int(os.getenv("BREATHER_EVERY", "14"))
BREATHER = os.getenv("BREATHER", "90-240")

_actions = 0


def _range(spec, default=(2.0, 5.0)):
    """Разобрать «20-60» в пару чисел. Кривое значение не должно ронять прогон."""
    try:
        lo, hi = (float(x) for x in str(spec).split("-", 1))
        return (lo, hi) if lo <= hi else (hi, lo)
    except Exception:
        return default


def _sleep(lo, hi):
    """Пауза со смещением к меньшему краю.

    Равномерное распределение даёт неестественно много длинных пауз.
    У человека большинство действий быстрые, а долгие — редкие, поэтому
    берём меньшее из двух бросков и изредка добавляем задержку.
    """
    a, b = random.uniform(lo, hi), random.uniform(lo, hi)
    t = min(a, b)
    if random.random() < 0.15:          # иногда человек всё же залипает
        t = max(a, b) + random.uniform(0, (hi - lo) * 0.3)
    time.sleep(t)
    return t


def between():
    """Пауза между карточками, изредка — большой перерыв."""
    global _actions
    _actions += 1
    if BREATHER_EVERY and _actions % BREATHER_EVERY == 0:
        lo, hi = _range(BREATHER, (90, 240))
        return _sleep(lo, hi)
    lo, hi = _range(STEP_PAUSE, (4, 10))
    return _sleep(lo, hi)


def after_apply():
    """Пауза после отправленного отклика — самая длинная."""
    lo, hi = _range(APPLY_PAUSE, (20, 60))
    return _sleep(lo, hi)


def settle(page):
    """Осмотреться перед кликом: чуть прокрутить страницу и выждать.

    Клик ровно в тот же миг, как элемент попал в поле зрения, и всегда
    в одну и ту же точку — самое машинное, что есть в прогоне.
    """
    try:
        if random.random() < 0.6:
            page.mouse.wheel(0, random.randint(-180, 260))
            time.sleep(random.uniform(0.3, 1.1))
        if random.random() < 0.25:      # иногда возвращаемся взглядом назад
            page.mouse.wheel(0, -random.randint(60, 200))
            time.sleep(random.uniform(0.2, 0.7))
    except Exception:
        pass                            # прокрутка — украшение, не работа
    time.sleep(random.uniform(0.4, 1.6))


def click(el):
    """Клик не строго в центр элемента.

    Playwright по умолчанию бьёт точно в середину — у человека так не
    выходит никогда. Промахнуться нельзя, поэтому смещаемся в пределах
    кнопки: берём её размер и отступаем от центра на четверть.
    """
    try:
        box = el.bounding_box()
        if box and box["width"] > 8 and box["height"] > 8:
            dx = box["width"] * random.uniform(-0.25, 0.25)
            dy = box["height"] * random.uniform(-0.25, 0.25)
            el.click(position={"x": box["width"] / 2 + dx,
                               "y": box["height"] / 2 + dy})
            return
    except Exception:
        pass
    el.click()
