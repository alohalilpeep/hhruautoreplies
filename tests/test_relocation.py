"""
Тесты подтверждения «вы откликаетесь на вакансию в другой стране».

Почему именно на это есть тест. Ошибка тут стоит дорого и оба раза тихая:
без нажатия подтверждения отклик не уходит, а страница выглядит прежней —
вакансия записывается как unknown и теряется. А первая версия исправления
искала кнопку по слову «откликнуться», которым подписаны кнопки в каждой
из полусотни карточек выдачи, — клик мог уйти в случайную вакансию.

Браузер тут не нужен: подменяем страницу заглушкой и смотрим, по чему
именно кликнули.

    venv/bin/python tests/test_relocation.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hh_searchapply as sa

sa.hh.pause = lambda *a, **k: None          # тесты не ждут


class Element:
    def __init__(self, name, page):
        self.name, self.page = name, page

    def click(self, **kw):          # настоящий click принимает timeout и position
        self.page.clicked.append(self.name)


class Locator:
    """Набор найденных элементов. Повторяет ту часть Playwright,
    которой пользуется confirm_relocation."""

    def __init__(self, names, page):
        self.names, self.page = list(names), page

    def count(self):
        return len(self.names)

    @property
    def first(self):
        return Element(self.names[0], self.page)

    def get_by_role(self, role, name=None):
        found = [n for n in self.names
                 if name is None or name.search(n)]
        return Locator(found, self.page)


class FakePage:
    """Страница, где по каждому селектору лежит заранее заданный список
    подписей элементов."""

    def __init__(self, by_selector, dialog_buttons=()):
        self.by_selector = by_selector
        self.dialog_buttons = list(dialog_buttons)
        self.clicked = []

    def locator(self, sel):
        if sel in ('[role="dialog"], [data-qa="bottom-sheet-content"]',):
            return Locator(self.dialog_buttons, self)
        return Locator(self.by_selector.get(sel, []), self)


CONFIRM = '[data-qa="relocation-warning-confirm"]'
CARDS = ["Откликнуться"] * 50          # кнопки в карточках выдачи


def test_нажимает_подтверждение_по_data_qa():
    page = FakePage({CONFIRM: ["Всё равно откликнуться"]})
    assert sa.confirm_relocation(page) is True
    assert page.clicked == ["Всё равно откликнуться"]


def test_находит_подтверждение_внутри_окна_без_data_qa():
    page = FakePage({CONFIRM: []},
                    dialog_buttons=["Отмена", "Всё равно откликнуться"])
    assert sa.confirm_relocation(page) is True
    assert page.clicked == ["Всё равно откликнуться"]


def test_не_трогает_кнопки_карточек_когда_окна_нет():
    """Главный тест: без всплывающего окна не должно быть ни одного клика.

    Если сюда просочится поиск по слову «откликнуться» по всей странице,
    тест упадёт — а в бою это был бы отклик на случайную вакансию.
    """
    page = FakePage({CONFIRM: [], "card-buttons": CARDS})
    assert sa.confirm_relocation(page) is False
    assert page.clicked == []


def test_окно_есть_но_подтверждения_в_нём_нет():
    page = FakePage({CONFIRM: []}, dialog_buttons=["Отмена", "Закрыть"])
    assert sa.confirm_relocation(page) is False
    assert page.clicked == []


def test_слово_откликнуться_не_считается_подтверждением():
    """В окне может быть обычная кнопка отклика — она не подтверждение."""
    page = FakePage({CONFIRM: []}, dialog_buttons=["Откликнуться"])
    assert sa.confirm_relocation(page) is False
    assert page.clicked == []


def test_сбой_клика_не_роняет_прогон():
    page = FakePage({CONFIRM: ["Подтвердить"]})

    def explode(**kw):
        raise RuntimeError("element is not attached")

    Element.click = explode
    try:
        assert sa.confirm_relocation(page) is False
    finally:
        Element.click = lambda self, **kw: self.page.clicked.append(self.name)


# --- реакция на клик -------------------------------------------------------

class ReactPage:
    """Страница для проверки apply_reacted: важно только, что где видно."""

    def __init__(self, visible_sel=(), url="https://hh.ru/search/vacancy",
                 texts=()):
        self.visible_sel = set(visible_sel)
        self.url = url
        self.texts = list(texts)
        self.context = type("Ctx", (), {"pages": [self]})()

    def locator(self, sel):
        return Locator(["есть"] if sel in self.visible_sel else [], self)

    def get_by_text(self, pattern):
        found = [x for x in self.texts if pattern.search(x)]
        return Locator(found, self)

    def is_visible(self):
        return True


def _reacted(page):
    hh = sa.hh
    hh.visible = lambda pg, sel: sel in pg.visible_sel
    return hh.apply_reacted(page, pages_before=1)


def test_окно_про_другую_страну_считается_реакцией():
    """Тот самый дефект: без этого клик признавался несработавшим,
    повторялся трижды, и зарубежная вакансия уходила в no_reaction."""
    page = ReactPage(visible_sel={sa.hh.SEL["relocation_confirm"]})
    assert _reacted(page) is True


def test_пустая_страница_реакцией_не_считается():
    assert _reacted(ReactPage()) is False


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ок    {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  ПАДАЕТ {t.__name__}: {e or 'проверка не прошла'}")
        except Exception as e:
            failed += 1
            print(f"  ОШИБКА {t.__name__}: {type(e).__name__}: {e}")
    print(f"\nпройдено {len(tests) - failed} из {len(tests)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
