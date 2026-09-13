"""
Пул вопросов работодателей, собранный автооткликами.

Вопросы копятся сами: когда hh_autoapply.py встречает вакансию с вопросами,
он её откладывает (статус questions) и складывает вопросы в таблицу questions.

    venv/bin/python hh_questions.py            частые вопросы
    venv/bin/python hh_questions.py --all      все вопросы подряд
    venv/bin/python hh_questions.py --vacancy  сгруппировано по вакансиям
"""
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

DB = Path(__file__).with_name("hh_responses.db")

# вопросы, которые повторяются у разных работодателей, стоит отвечать шаблоном
TOPICS = {
    "зарплата": r"зарплат|заработн|доход|вилк|ожидани\w* по\b|сколько хотите",
    "город / переезд": r"город\w*|переезд|релокац|проживае|живёте|живете",
    "гражданство": r"гражданств|патент|разрешени\w* на работу",
    "контакты": r"telegram|телеграм|@|номер телефона|связаться",
    "опыт (лет)": r"сколько лет|опыт работы|стаж",
    "готовность выйти": r"когда готов|выйти на работу|срок|испытательн",
    "занятость": r"занятост|совмещ|полный день|part-?time|подработ",
    "английский": r"английск|english|язык",
    "образование": r"образован|вуз|универс",
}


def norm(q):
    """Свести вопрос к сравнимому виду: без регистра, пунктуации и лишних пробелов."""
    q = q.lower().replace("\xa0", " ")
    q = re.sub(r"[^\w\s]", " ", q)
    return re.sub(r"\s+", " ", q).strip()


def topic_of(q):
    low = norm(q)
    for name, pat in TOPICS.items():
        if re.search(pat, low):
            return name
    return None


def main():
    if not DB.exists():
        raise SystemExit("Базы нет, сначала запусти hh_autoapply.py")
    db = sqlite3.connect(DB)
    try:
        rows = db.execute(
            "SELECT vacancy_id, company, question, kind, options FROM questions"
        ).fetchall()
    except sqlite3.OperationalError:
        raise SystemExit("Таблицы questions ещё нет, запусти hh_autoapply.py")

    if not rows:
        raise SystemExit("Пул пуст: вакансий с вопросами пока не встречалось")

    vacancies = {r[0] for r in rows}
    print(f"Вопросов: {len(rows)} | вакансий с вопросами: {len(vacancies)}\n")

    if "--all" in sys.argv:
        for vid, comp, q, kind, opts in rows:
            print(f"[{vid}] {comp}\n  {q}\n  поле: {kind}"
                  + (f" | варианты: {opts}" if opts else "") + "\n")
        return

    if "--vacancy" in sys.argv:
        by_vac = {}
        for vid, comp, q, kind, opts in rows:
            by_vac.setdefault((vid, comp), []).append(q)
        for (vid, comp), qs in sorted(by_vac.items(), key=lambda x: -len(x[1])):
            print(f"=== {comp} ({vid}) — {len(qs)} вопросов")
            for q in qs:
                print(f"   • {q.splitlines()[0][:100]}")
            print()
        return

    # сколько раз встречался дословно одинаковый вопрос
    exact = Counter(norm(r[2]) for r in rows)
    repeated = [(n, q) for q, n in exact.items() if n > 1]
    print("=== дословные повторы ===")
    if repeated:
        for n, q in sorted(repeated, reverse=True):
            print(f"  {n}x  {q[:110]}")
    else:
        print("  пока нет")

    print("\n=== по темам (что можно отвечать шаблоном) ===")
    topics = Counter()
    examples = {}
    for _, _, q, _, _ in rows:
        t = topic_of(q)
        if t:
            topics[t] += 1
            examples.setdefault(t, q.splitlines()[0][:100])
    if topics:
        for t, n in topics.most_common():
            print(f"  {n:3d}x  {t}")
            print(f"        напр.: {examples[t]}")
    else:
        print("  пока нет")

    unmatched = [r[2] for r in rows if not topic_of(r[2])]
    print(f"\n=== вне тем: {len(unmatched)} "
          f"(обычно уникальные техвопросы работодателя) ===")
    for q in unmatched[:10]:
        print(f"  • {q.splitlines()[0][:100]}")
    if len(unmatched) > 10:
        print(f"  ... ещё {len(unmatched) - 10}")


if __name__ == "__main__":
    main()
