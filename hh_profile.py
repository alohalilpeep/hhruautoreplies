"""
Данные о себе: имя, контакты, стаж, зарплата.

Зачем. Эти факты и так рассыпаны по банку ответов — в одном ответе стаж,
в другом почта, в третьем вуз. Когда отвечаешь на новую пачку вопросов,
их не видно, и легко написать «4 года» там, где в резюме стоит шесть.
Теперь они собраны в одном файле и печатаются в шапке выгрузки.

Скрипт ничего не подставляет сам: он только показывает, что задано.
Отвечает по-прежнему человек — иначе в анкету уедет выдумка.

Файл `profile.txt` лежит рядом со скриптами, строки вида «ключ: значение».
У каждого аккаунта он свой: телеграм и стаж там разные.
"""
import re
from pathlib import Path

PROFILE_FILE = Path(__file__).with_name("profile.txt")

# Порядок, в котором поля показываются. Что не перечислено — ниже, как есть.
ORDER = ["фио", "дата рождения", "возраст", "город", "гражданство",
         "почта", "телеграм", "телефон",
         "образование", "специальность", "вуз",
         "опыт", "зарплата", "формат", "занятость"]


def load(path=None):
    """Прочитать профиль в пары «ключ — значение». Нет файла — пусто."""
    p = Path(path) if path else PROFILE_FILE
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip().lower(), v.strip()
        if k and v:
            out.append((k, v))
    known = {k: v for k, v in out}
    ordered = [(k, known[k]) for k in ORDER if k in known]
    ordered += [(k, v) for k, v in out if k not in ORDER]
    return ordered


def render(path=None, width=16):
    """Блок для шапки выгрузки. Пустой профиль — пустая строка."""
    rows = load(path)
    if not rows:
        return ""
    lines = ["# ЧТО СЕЙЧАС ЗАДАНО О СЕБЕ (из profile.txt):"]
    for k, v in rows:
        lines.append(f"#   {k + ':':<{width}} {v}")
    lines.append("#")
    lines.append("# Это справка, чтобы ответы не противоречили друг другу и резюме.")
    lines.append("# Скрипт сам эти значения в анкеты не подставляет.")
    return "\n".join(lines)


def check(path=None):
    """Чего не хватает. Возвращает список пустых обязательных полей."""
    have = {k for k, _ in load(path)}
    must = ["фио", "почта", "телеграм", "опыт", "зарплата"]
    return [m for m in must if m not in have]


if __name__ == "__main__":
    print(render() or "profile.txt пуст или отсутствует")
    missing = check()
    if missing:
        print("\nне заполнено:", ", ".join(missing))
