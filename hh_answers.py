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
    venv/bin/python hh_answers.py --export      выгрузить в answers_edit.txt
    venv/bin/python hh_answers.py --import      вернуть правки из txt в базу
    venv/bin/python hh_answers.py --list        показать банк
    venv/bin/python hh_answers.py --list draft  только неготовые
    venv/bin/python hh_answers.py --approve-all пометить готовыми черновики
    venv/bin/python hh_answers.py --block "X"   заблокировать компанию
    venv/bin/python hh_answers.py --purge "X"   заблокировать и вычистить её вопросы
    venv/bin/python hh_answers.py               сводка

Обычный цикл: --fill → --export → правишь txt → --import
"""
import datetime as dt
import hashlib
import re
import sqlite3
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from hh_autoapply import init_db, normalize_question, classify_question

EDIT_FILE = Path(__file__).with_name("answers_edit.txt")
SEP = "-" * 3

# что писать в СТАТУС. Первое значение — каноническое, остальные синонимы
STATUS_WORDS = {
    "approved": {"approved", "готов", "готово", "ok", "ок", "да", "+"},
    "draft": {"draft", "черновик", "-"},
    "needs_input": {"needs_input", "нужен", "надо", "?"},
    "skip": {"skip", "пропуск", "нет", "мимо", "х"},
}

BLACKLIST_FILE = Path(__file__).with_name("blacklist.txt")

HEADER = """\
# Ответы на вопросы работодателей. Экспорт из hh_responses.db.
#
# Правь ТОЛЬКО текст после «ОТВЕТ:» и, если нужно, строку «СТАТУС:».
# Строку «ID:» не трогай — по ней ответ возвращается на место.
# Блоки разделены строкой из трёх дефисов. Порядок блоков не важен.
#
# УДАЛИТЬ БЛОК = больше не откликаться на вакансии с этим вопросом.
# Вопрос уходит в статус skip, вакансия пропускается целиком и навсегда.
# Считаются только блоки, выгруженные в ЭТОТ файл (список ниже, в EXPORTED),
# так что частичная выгрузка ничего лишнего не удалит.
#
# Компанию целиком блокируй в blacklist.txt — одна строка на компанию,
# совпадение по части названия. Или: hh_answers.py --block "Название"
#
# СТАТУС:
#   approved    ответ уйдёт работодателю   (синонимы: готов, ок, да, +)
#   draft       черновик, не отправляется  (синонимы: черновик, -)
#   needs_input нужен твой ответ           (синонимы: нужен, ?)
#   skip        не откликаться на такие вакансии
#
# Ответ может быть в несколько строк — читается всё до следующего разделителя.
# Вернуть в базу:  venv/bin/python hh_answers.py --import
"""


def qid(qnorm):
    """Короткий стабильный якорь вопроса для txt-файла."""
    return hashlib.sha1(qnorm.encode("utf-8")).hexdigest()[:8]


def companies_for(db):
    """Кто спрашивал каждый вопрос. Один вопрос встречается у разных компаний."""
    out = {}
    for company, question in db.execute(
            "SELECT company, question FROM questions"):
        out.setdefault(normalize_question(question), []).append(
            (company or "").strip() or "?")
    return {k: sorted(set(v)) for k, v in out.items()}


def export(db, path=EDIT_FILE):
    comps = companies_for(db)
    rows = db.execute(
        "SELECT qnorm, question, answer, status FROM answer_bank"
    ).fetchall()
    # сначала то, что требует внимания
    order = {"needs_input": 0, "draft": 1, "approved": 2}
    rows.sort(key=lambda r: (order.get(r[3], 3), r[1]))

    # список выгруженных ID: по нему импорт поймёт, какие блоки удалены,
    # и не примет за удаление то, что просто не выгружалось
    ids = [qid(qn) for qn, *_ in rows]
    exported = "\n".join(
        "# EXPORTED: " + " ".join(ids[i:i + 8]) for i in range(0, len(ids), 8))

    chunks = [HEADER + exported + "\n"]
    for qnorm, question, answer, status in rows:
        flat = re.sub(r"\s+", " ", (question or "").replace("\xa0", " ")).strip()
        wrapped = textwrap.fill(flat, width=96, subsequent_indent="        ")
        chunks.append(
            f"{SEP}\n"
            f"ID: {qid(qnorm)}\n"
            f"СТАТУС: {status}\n"
            f"КОМПАНИЯ: {', '.join(comps.get(qnorm, [])) or '—'}\n"
            f"ВОПРОС: {wrapped}\n"
            f"ОТВЕТ:\n"
            f"{(answer or '').strip()}\n")
    chunks.append(SEP + "\n")

    path.write_text("\n".join(chunks), encoding="utf-8")
    counts = {}
    for *_, status in rows:
        counts[status] = counts.get(status, 0) + 1
    print(f"выгружено {len(rows)} вопросов в {path.name}")
    for s, n in sorted(counts.items()):
        print(f"  {s:12s} {n}")
    print(f"\nправь и возвращай:  venv/bin/python hh_answers.py --import")


def _parse_status(raw):
    # в строке статуса может быть пометка в скобках — она не мешает
    raw = re.sub(r"[\[(].*", "", raw or "").strip()
    low = raw.split()[0].lower() if raw.split() else ""
    for canon, words in STATUS_WORDS.items():
        if low in words:
            return canon
    return None


def import_(db, path=EDIT_FILE):
    if not path.exists():
        raise SystemExit(f"нет файла {path.name} — сначала --export")

    by_id = {qid(qn): qn for (qn,) in db.execute("SELECT qnorm FROM answer_bank")}
    text = path.read_text(encoding="utf-8")
    blocks = re.split(rf"^{SEP}\s*$", text, flags=re.M)

    # что было выгружено в этот файл — база для вычисления удалённого
    exported_ids = set()
    for line in text.splitlines():
        if line.startswith("# EXPORTED:"):
            exported_ids.update(line.split(":", 1)[1].split())

    present_ids = set()
    updated, skipped, unknown, bad_status = 0, 0, [], []
    for block in blocks:
        lines = block.splitlines()
        fields, answer_lines, in_answer = {}, [], False
        for line in lines:
            if in_answer:
                answer_lines.append(line)
                continue
            if line.strip() == "ОТВЕТ:":
                in_answer = True
                continue
            if line.startswith("#") or not line.strip():
                continue
            m = re.match(r"^(ID|СТАТУС|КОМПАНИЯ|ВОПРОС):\s*(.*)$", line)
            if m:
                fields[m.group(1)] = m.group(2)

        ident = fields.get("ID", "").strip()
        if not ident:
            continue
        present_ids.add(ident)
        qnorm = by_id.get(ident)
        if not qnorm:
            unknown.append(ident)
            continue

        # комментарии-разделители, случайно попавшие в хвост ответа
        while answer_lines and answer_lines[-1].lstrip().startswith("#"):
            answer_lines.pop()
        answer = "\n".join(answer_lines).strip()
        status = _parse_status(fields.get("СТАТУС"))
        if fields.get("СТАТУС") and status is None:
            bad_status.append((ident, fields["СТАТУС"].strip()))
            continue
        if status == "approved" and not answer:
            bad_status.append((ident, "approved с пустым ответом"))
            continue

        cur = db.execute(
            "UPDATE answer_bank SET answer=?, status=?, ts=? "
            "WHERE qnorm=? AND (answer IS NOT ? OR status IS NOT ?)",
            (answer, status or "draft",
             dt.datetime.now().isoformat(timespec="seconds"),
             qnorm, answer, status or "draft"))
        if cur.rowcount:
            updated += 1
        else:
            skipped += 1
    # блоки, которые были выгружены, но в файле их больше нет = удалены
    deleted = [by_id[i] for i in sorted(exported_ids - present_ids) if i in by_id]
    for qnorm in deleted:
        db.execute(
            "UPDATE answer_bank SET status='skip', ts=? WHERE qnorm=?",
            (dt.datetime.now().isoformat(timespec="seconds"), qnorm))
    db.commit()

    print(f"обновлено: {updated}, без изменений: {skipped}")
    if deleted:
        print(f"\nудалено из файла → больше не откликаемся ({len(deleted)}):")
        for qnorm in deleted[:12]:
            q = db.execute("SELECT question FROM answer_bank WHERE qnorm=?",
                           (qnorm,)).fetchone()[0]
            flat = re.sub(r"\s+", " ", q or "").strip()
            print(f"  • {flat[:95]}")
        if len(deleted) > 12:
            print(f"  ... ещё {len(deleted) - 12}")
    if unknown:
        print(f"\nнеизвестные ID ({len(unknown)}) — пропущены: {unknown[:10]}")
    if bad_status:
        print(f"\nне принято ({len(bad_status)}):")
        for ident, why in bad_status:
            print(f"  {ident}: {why}")
    print()
    stats(db)


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
            "INSERT INTO answer_bank (qnorm, question, answer, status, ts) "
            "VALUES (?,?,?,?,?)",
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


def block_company(name):
    """Добавить компанию в стоп-лист. Совпадение по части названия."""
    name = name.strip()
    if not name:
        raise SystemExit("укажи название: --block \"Название компании\"")
    existing = []
    if BLACKLIST_FILE.exists():
        existing = [l.strip() for l in
                    BLACKLIST_FILE.read_text(encoding="utf-8").splitlines()]
    if any(l.lower() == name.lower() for l in existing if l):
        print(f"{name!r} уже в стоп-листе")
        return
    with BLACKLIST_FILE.open("a", encoding="utf-8") as f:
        if not existing:
            f.write("# Компании, которым не откликаемся. Одна строка на компанию,\n"
                    "# совпадение по части названия, регистр не важен.\n")
        f.write(name + "\n")
    print(f"{name!r} добавлена в {BLACKLIST_FILE.name}")


def purge_company(db, name):
    """Заблокировать компанию и вычистить её вопросы из базы.

    Вопросы, которые задаёт кто-то ещё, не трогаем: ответ на них может быть
    нужен для другой вакансии.
    """
    name = name.strip()
    if not name:
        raise SystemExit('укажи название: --purge "Название компании"')
    block_company(name)

    like = f"%{name}%"
    theirs = {normalize_question(q) for (q,) in db.execute(
        "SELECT question FROM questions WHERE company LIKE ?", (like,))}
    shared = {normalize_question(q) for (q,) in db.execute(
        "SELECT question FROM questions WHERE company NOT LIKE ?", (like,))}
    only = theirs - shared

    nq = db.execute("DELETE FROM questions WHERE company LIKE ?", (like,)).rowcount
    nb = sum(db.execute("DELETE FROM answer_bank WHERE qnorm=?", (q,)).rowcount
             for q in only)
    try:
        nc = db.execute("DELETE FROM chat_questions WHERE company LIKE ?",
                        (like,)).rowcount
    except sqlite3.OperationalError:
        nc = 0
    db.commit()
    print(f"удалено вопросов: {nq}, ответов из банка: {nb}, реплик из чатов: {nc}")
    if theirs - only:
        print(f"оставлено общих с другими компаниями: {len(theirs - only)}")
    print("\nне забудь перевыгрузить файл: hh_answers.py --export")


def main():
    db = init_db()
    args = sys.argv[1:]
    if "--purge" in args:
        i = args.index("--purge")
        purge_company(db, " ".join(args[i + 1:]))
        return
    if "--block" in args:
        i = args.index("--block")
        block_company(" ".join(args[i + 1:]))
        return
    path = EDIT_FILE
    for a in args:
        if a.endswith(".txt"):
            path = Path(a)
    if "--fill" in args:
        fill(db)
    elif "--export" in args:
        export(db, path)
    elif "--import" in args:
        import_(db, path)
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
