"""
Банк ответов на вопросы работодателей.

Схема работы:
  1. hh_autoapply.py откладывает вакансии с вопросами и копит их в таблице
     questions — это происходит само.
  2. --fill заводит в answer_bank строку на каждый уникальный вопрос.
  3. Ответы пишутся в answer_bank.answer. Правь их прямо в базе:
        sqlite3 hh_responses.db
        UPDATE answer_bank SET answer='...' WHERE qnorm LIKE '%linux%';
     или любым графическим клиентом (DB Browser for SQLite и т.п.).
  4. --approve помечает ответы готовыми. Отправляются ТОЛЬКО approved:
     пока ответ в статусе draft, вакансия просто откладывается.

    venv/bin/python hh_answers.py --fill        завести строки под новые вопросы
    venv/bin/python hh_answers.py --list        показать банк
    venv/bin/python hh_answers.py --list draft  только неготовые
    venv/bin/python hh_answers.py --approve-all пометить готовыми все непустые
    venv/bin/python hh_answers.py --stats       сводка
"""
import datetime as dt
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from hh_autoapply import init_db, normalize_question, classify_question


def fill(db):
    """Завести в банке строку на каждый уникальный вопрос из пула."""
    seen = {q for (q,) in db.execute("SELECT qnorm FROM answer_bank")}
    added = 0
    for (question,) in db.execute("SELECT question FROM questions"):
        qn = normalize_question(question)
        if not qn or qn in seen:
            continue
        seen.add(qn)
        db.execute(
            "INSERT INTO answer_bank VALUES (?,?,?,?,?)",
            (qn, question.strip(), "", "draft",
             dt.datetime.now().isoformat(timespec="seconds")))
        added += 1
    db.commit()
    print(f"заведено новых вопросов: {added}, всего в банке: {len(seen)}")


def show(db, only=None):
    rows = db.execute(
        "SELECT qnorm, question, answer, status FROM answer_bank ORDER BY status, qnorm"
    ).fetchall()
    for qn, question, answer, status in rows:
        if only and status != only:
            continue
        topic = classify_question(question)
        mark = "OK " if status == "approved" else "   "
        print(f"\n{mark}[{status}]" + (f" тема={topic}" if topic else ""))
        print(f"  В: {question.strip()[:150]}")
        if answer and answer.strip():
            print(f"  О: {answer.strip()[:300]}")
        else:
            print("  О: (пусто)")


def approve_all(db):
    """Одобрить только черновики. Статус needs_input не трогаем: там ответ
    требует личных данных или опыта, которых нет в резюме, и одобрять его
    оптом нельзя."""
    cur = db.execute(
        "UPDATE answer_bank SET status='approved' "
        "WHERE status='draft' AND TRIM(COALESCE(answer,'')) != ''")
    db.commit()
    print(f"помечено готовыми: {cur.rowcount}")
    for status, n in db.execute(
            "SELECT status, COUNT(*) FROM answer_bank "
            "WHERE status != 'approved' GROUP BY status"):
        print(f"  осталось в статусе {status}: {n}")
    print("\nneeds_input одобряй только вручную, по одному:")
    print("  UPDATE answer_bank SET status='approved' WHERE qnorm LIKE '%...%';")


def stats(db):
    total = db.execute("SELECT COUNT(*) FROM answer_bank").fetchone()[0]
    print(f"вопросов в банке: {total}")
    for status, n in db.execute(
            "SELECT status, COUNT(*) FROM answer_bank GROUP BY status"):
        print(f"  {status:10s} {n}")
    empty = db.execute(
        "SELECT COUNT(*) FROM answer_bank WHERE TRIM(COALESCE(answer,'')) = ''"
    ).fetchone()[0]
    print(f"  без текста ответа: {empty}")

    # сколько вакансий станет отвечаемыми
    bank = {q for (q,) in db.execute(
        "SELECT qnorm FROM answer_bank WHERE status='approved' "
        "AND TRIM(COALESCE(answer,'')) != ''")}
    by_vac = {}
    for vid, comp, question in db.execute(
            "SELECT vacancy_id, company, question FROM questions"):
        by_vac.setdefault((vid, comp), []).append(question)
    ready = [(c, len(qs)) for (v, c), qs in by_vac.items()
             if all(normalize_question(q) in bank or classify_question(q)
                    for q in qs)]
    print(f"\nвакансий закрыто целиком: {len(ready)} из {len(by_vac)}")
    for c, n in sorted(ready, key=lambda x: -x[1]):
        print(f"  {c} — {n} вопр.")


def main():
    db = init_db()
    args = sys.argv[1:]
    if "--fill" in args:
        fill(db)
    elif "--approve-all" in args:
        approve_all(db)
    elif "--list" in args:
        i = args.index("--list")
        only = args[i + 1] if len(args) > i + 1 else None
        show(db, only)
    else:
        stats(db)


if __name__ == "__main__":
    main()
