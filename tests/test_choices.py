"""
Вопросы с выбором варианта: ключ «текст + варианты» и «Свой вариант».

    venv/bin/python tests/test_choices.py

Без браузера: база во временном файле, страница не нужна.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hh_autoapply as hh  # noqa: E402
import hh_choices  # noqa: E402

FORMAT = "Какой формат работы вы рассматриваете?"
OLD = ["Полностью удаленный (Все города РФ)", "Гибрид", "Офис"]
NEW = ["Офис", "Гибрид", "Удаленный"]
SAME = ["Офис", "Полностью удаленный (Все города РФ)"]


def fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return hh.init_db(path)


def legacy_db():
    """База старого вида: выбор одобрен по одному тексту вопроса."""
    db = fresh_db()
    db.execute(
        "INSERT INTO answer_bank (qnorm, question, answer, status, ts, kind, options, "
        "choice, choice_status) VALUES (?,?,?,?,?,?,?,?,?)",
        (hh.normalize_question(FORMAT), FORMAT, "Удалённо, живу в Москве", "approved",
         "2026-10-01", "radio", " | ".join(OLD), OLD[0], "approved"))
    for vid, opts in (("1", OLD), ("2", NEW), ("3", SAME)):
        db.execute("INSERT INTO questions VALUES (?,?,?,?,?,?,?,?)",
                   (vid, "", "", 0, FORMAT, "radio", " | ".join(opts), ""))
    db.commit()
    hh.split_choice_keys(db)
    return db


def row(db, opts):
    return db.execute("SELECT choice, choice_status FROM answer_bank WHERE qnorm=?",
                      (hh.bank_key(FORMAT, "radio", opts),)).fetchone()


def test_ключ_зависит_от_вариантов_а_не_от_их_порядка():
    assert hh.bank_key(FORMAT, "radio", OLD) != hh.bank_key(FORMAT, "radio", NEW)
    assert hh.bank_key(FORMAT, "radio", OLD) == hh.bank_key(FORMAT, "radio", OLD[::-1])
    assert hh.bank_key(FORMAT, "textarea", OLD) == hh.normalize_question(FORMAT)
    assert hh.base_of(hh.bank_key(FORMAT, "radio", OLD)) == hh.normalize_question(FORMAT)


def test_старый_выбор_остаётся_у_своего_набора_вариантов():
    assert row(legacy_db(), OLD) == (OLD[0], "approved")


def test_другой_набор_без_той_подписи_уходит_человеку():
    choice, status = row(legacy_db(), NEW)
    assert not choice and status != "approved"


def test_набор_с_той_же_подписью_наследует_выбор():
    assert row(legacy_db(), SAME) == (OLD[0], "approved")


def test_текстовый_ответ_старой_строки_не_теряется():
    db = legacy_db()
    assert hh.load_answer_bank(db)[hh.normalize_question(FORMAT)] == "Удалённо, живу в Москве"
    opts = db.execute("SELECT options FROM answer_bank WHERE qnorm=?",
                      (hh.normalize_question(FORMAT),)).fetchone()[0]
    assert opts is None                 # во вторую часть выгрузки не попадёт


def test_повторный_перевод_ничего_не_меняет():
    db = legacy_db()
    before = db.execute("SELECT * FROM answer_bank ORDER BY qnorm").fetchall()
    hh.split_choice_keys(db)
    assert db.execute("SELECT * FROM answer_bank ORDER BY qnorm").fetchall() == before


def _import(db, key, choice, own_lines):
    fd, path = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    Path(path).write_text(
        "---\n"
        f"КЛЮЧ: {key}\nСТАТУС: approved\nТИП: radio,textarea\nВОПРОС: {FORMAT}\n"
        f"ВАРИАНТЫ: {' | '.join(NEW + ['Свой вариант'])}\nВЫБОР: {choice}\n"
        + "".join(l + "\n" for l in own_lines) + "---\n", encoding="utf-8")
    hh_choices.import_(db, path)


def _open_db():
    db = fresh_db()
    hh.remember_question(db, {"question": FORMAT, "kind": "radio,textarea",
                              "options": NEW + ["Свой вариант"]})
    return db, hh.bank_key(FORMAT, "radio,textarea", NEW + ["Свой вариант"])


def test_свой_вариант_с_текстом_сохраняется():
    db, key = _open_db()
    _import(db, key, "Свой вариант", ["Удалёнка или гибрид", "1–3 дня в офисе"])
    assert hh.load_choice_bank(db)[key] == ["Свой вариант"]
    assert hh.load_choice_texts(db)[key] == "Удалёнка или гибрид\n1–3 дня в офисе"


def test_свой_вариант_без_текста_не_принимается():
    db, key = _open_db()
    _import(db, key, "Свой вариант", [])
    assert key not in hh.load_choice_bank(db)


def test_текст_под_обычным_вариантом_не_вписывается():
    db, key = _open_db()
    _import(db, key, "Гибрид", ["пояснение для себя"])
    assert key in hh.load_choice_bank(db) and key not in hh.load_choice_texts(db)


def _page_q():
    return {"kind": "radio,textarea", "choices": [
        {"label": "Офис", "value": "1"}, {"label": "Свой вариант", "value": "open"}]}


def test_свой_вариант_без_текста_кликер_не_выбирает():
    assert hh.plan_choice(_page_q(), ["Свой вариант"]) is None


def test_свой_вариант_с_текстом_кликер_выбирает():
    chosen = hh.plan_choice(_page_q(), ["Свой вариант"], "гибрид")
    assert [c["value"] for c in chosen] == ["open"]


def main():
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ок    {name}")
        except AssertionError as e:
            failed += 1
            print(f"  СБОЙ  {name} {e}")
    print(f"\nпройдено {len(tests) - failed} из {len(tests)}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
