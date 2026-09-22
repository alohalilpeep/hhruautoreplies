"""
Сбор вариантов ответа из анкет работодателей.

Часть анкет hh — не свободный текст, а выбор варианта: радиокнопки и
чекбоксы. Отвечать на них скрипт отказывается, пока вариант не одобрен
человеком, поэтому варианты сначала надо собрать и показать.

    python hh_choices.py --scan     обойти отложенные вакансии и снять варианты
    python hh_choices.py --list     показать, что собрано

Форма отклика открывается прямой ссылкой, без нажатия «Откликнуться»:
на части вакансий hh отправляет отклик мгновенно по клику, и кликать
ради сбора данных нельзя. Ничего не заполняется и не отправляется.
"""
import re
import sys

from playwright.sync_api import sync_playwright

import hh_autoapply as hh

# статусы, после которых возвращаться к вакансии незачем
FINAL = ("applied", "answered", "already", "blacklisted", "external",
         "no_button", "skipped_question")

FORM_URL = "https://hh.ru/applicant/vacancy_response?vacancyId={}"


def targets(db):
    """Вакансии с анкетой, по которым решение ещё не принято."""
    marks = ",".join("?" * len(FINAL))
    return db.execute(
        f"""SELECT DISTINCT q.vacancy_id, q.company FROM questions q
            WHERE COALESCE((SELECT status FROM responses r
                            WHERE r.id = q.vacancy_id), '') NOT IN ({marks})
            ORDER BY q.vacancy_id""", FINAL).fetchall()


def scan(db, limit=None):
    todo = targets(db)
    if limit:
        todo = todo[:limit]
    print(f"вакансий к обходу: {len(todo)}")
    if not todo:
        return

    ws = hh.open_profile()
    seen = gone = with_choice = 0
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(ws)
        page = browser.contexts[0].new_page()
        page.set_default_navigation_timeout(60000)
        for vid, company in todo:
            url = FORM_URL.format(vid)
            try:
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)
                qs = hh.scrape_questions(page)
            except Exception as e:
                print(f"  {vid} {company[:24]}: {type(e).__name__}")
                continue
            if not qs:
                gone += 1
                print(f"  {vid} {company[:24]}: анкета не открылась")
                continue
            seen += 1
            hh.save_questions(db, vid, f"https://hh.ru/vacancy/{vid}",
                              company, qs)
            remember(db, qs)
            n = sum(1 for q in qs if q.get("choices"))
            with_choice += bool(n)
            print(f"  {vid} {company[:24]:24} вопросов {len(qs)}, с выбором {n}")
        page.close()
    print(f"\nобойдено {seen}, из них с выбором {with_choice}, "
          f"не открылось {gone}")


def remember(db, questions):
    """Записать в банк тип поля и варианты. Ответы и статусы не трогаем."""
    for q in questions:
        qn = hh.normalize_question(q["question"])
        if not qn:
            continue
        opts = " | ".join(q["options"]) if q.get("options") else None
        row = db.execute("SELECT 1 FROM answer_bank WHERE qnorm=?",
                         (qn,)).fetchone()
        if row:
            db.execute("UPDATE answer_bank SET kind=?, options=? WHERE qnorm=?",
                       (q["kind"], opts, qn))
        else:
            db.execute(
                "INSERT INTO answer_bank (qnorm, question, answer, status, ts,"
                " kind, options) VALUES (?,?,?,?,?,?,?)",
                (qn, q["question"].strip(), "", "draft",
                 hh.dt.datetime.now().isoformat(timespec="seconds"),
                 q["kind"], opts))
    db.commit()


def show(db):
    rows = db.execute(
        "SELECT question, kind, options, answer, status FROM answer_bank "
        "WHERE options IS NOT NULL AND options != '' ORDER BY status, question"
    ).fetchall()
    print(f"вопросов с выбором варианта: {len(rows)}\n")
    for question, kind, options, answer, status in rows:
        print(f"[{status}] ({kind})")
        print(f"  В: {' '.join(question.split())[:90]}")
        print(f"  Варианты: {options}")
        print(f"  Выбрано: {answer or '—'}\n")


# Вопросы, где вариант — личный факт или твоё решение. Угадывать нельзя.
PERSONAL_RE = re.compile(
    r"воен\w*\s*билет|приписн|судимост|гражданств|допуск|гостайн"
    r"|тк\s*рф|официальн|гпх|самозанят|\bип\b|\bооо\b|оформлен"
    r"|командировк|выезд|разъезд|переезд|релокац"
    r"|офис|гибрид|город|находитесь|проживаете|локац"
    r"|смен\w*\s*график|ночн|2/2"
    r"|возраст|зарплат|вилк|доход|оклад", re.I)

# «Свой вариант» раскрывает скрытое поле — автоматически не выбираем
OPEN_RE = re.compile(r"^свой вариант$|^другое$|^не могу оценить$", re.I)

# Технологии из твоего резюме и одобренных ответов. Остальное в списках
# не отмечаем: Airflow, Hadoop, MLflow, GPU/CUDA, 1С, Supermicro и прочее
# ты добавишь сам, если опыт есть.
STACK_RE = re.compile(
    r"kubernetes|k8s|docker|terraform|ansible|helm|argo|flux|gitlab|jenkins"
    r"|nginx|haproxy|apache|prometheus|grafana|alertmanager|zabbix|vault"
    r"|nexus|postgres|mongo|clickhouse|redis|kafka|rabbit|linux|bash|python"
    r"|swarm|ceph|vmware|kvm|qemu|git\b", re.I)

YES_RE = re.compile(r"^\s*да\b", re.I)
NO_RE = re.compile(r"^\s*нет\b", re.I)


def split_options(raw):
    return [o.strip() for o in (raw or "").split("|") if o.strip()]


def derive(question, options, answer):
    """Черновик выбора из уже одобренного текстового ответа.

    Возвращает (выбор, статус). Уверены — draft на проверку, не уверены —
    needs_input, чтобы ты проставил руками. Пустой выбор не одобряем никогда.
    """
    opts = split_options(options)
    pickable = [o for o in opts if not OPEN_RE.match(o)]
    if not pickable:
        return "", "needs_input"
    if PERSONAL_RE.search(question):
        return "", "needs_input"

    low = {o.lower(): o for o in pickable}
    # чистое да/нет: тянем из текстового ответа
    if set(low) == {"да", "нет"} and answer:
        if YES_RE.match(answer):
            return low["да"], "draft"
        if NO_RE.match(answer):
            return low["нет"], "draft"

    # грейд: берём из текстового ответа, по умолчанию middle
    if any(re.search(r"middle|senior|junior", o, re.I) for o in pickable):
        want = "senior" if re.search(r"senior|сеньор", answer or "", re.I) \
            else "middle"
        for o in pickable:
            if re.search(want, o, re.I):
                return o, "draft"

    # шкала «1 (…) … 5 (…)»: опыт считаем по годам, знания — уверенный уровень
    scale = [o for o in pickable if re.match(r"^\s*\d+\s*[\(.]", o)]
    if len(scale) >= 4:
        if re.search(r"опыт|лет|стаж", " ".join(scale), re.I):
            for o in scale:                       # 4+ года коммерческого опыта
                if re.search(r"3\s*-\s*6|3-6|4|более\s*3", o, re.I):
                    return o, "draft"
        for o in scale:                           # уверенный, но не «профи»
            if re.match(r"^\s*4\b", o):
                return o, "draft"

    # список технологий: отмечаем то, с чем действительно работал
    known = [o for o in pickable if STACK_RE.search(o)]
    if known and len(pickable) >= 3:
        return " | ".join(known), "draft"
    return "", "needs_input"


def draft(db):
    rows = db.execute(
        "SELECT qnorm, question, options, answer, choice, choice_status, status "
        "FROM answer_bank WHERE options IS NOT NULL AND options != ''"
    ).fetchall()
    made = need = kept = 0
    for qnorm, question, options, answer, choice, cstatus, status in rows:
        if cstatus in ("approved", "skip"):
            kept += 1
            continue
        # Текстовый ответ на этот вопрос ты ещё не дал или отметил его как
        # «нет такого опыта». Выбор варианта тем более угадывать нельзя:
        # иначе шкала сама поставит «4 (опыт 3-6 лет)» там, где опыта нет.
        if status in ("needs_input", "skip") or "НУЖЕН ТВОЙ ОТВЕТ" in (answer or ""):
            db.execute("UPDATE answer_bank SET choice=?, choice_status=? "
                       "WHERE qnorm=?", ("", "needs_input", qnorm))
            need += 1
            continue
        pick, status = derive(question, options, answer or "")
        db.execute("UPDATE answer_bank SET choice=?, choice_status=? "
                   "WHERE qnorm=?", (pick, status, qnorm))
        if status == "draft":
            made += 1
        else:
            need += 1
    db.commit()
    print(f"черновиков выбора: {made}, требуют твоего ответа: {need}, "
          f"не тронуто (уже решено): {kept}")


EDIT_FILE = "choices_edit.txt"
HEAD = """# Выбор варианта в анкетах работодателей.
# Правь строки ВЫБОР и СТАТУС, потом: python hh_choices.py --import
#
# СТАТУС: approved — можно кликать, needs_input — жду твой ответ,
#         skip — не отвечать на этот вопрос никогда.
# ВЫБОР: скопируй подпись варианта ровно как в строке ВАРИАНТЫ.
#        Для чекбоксов можно несколько, через « | ».
# Отправляется только approved. Пустой выбор не отправляется никогда.
"""


def export(db, path=EDIT_FILE):
    rows = db.execute(
        "SELECT qnorm, question, kind, options, choice, choice_status "
        "FROM answer_bank WHERE options IS NOT NULL AND options != '' "
        "ORDER BY CASE choice_status WHEN 'needs_input' THEN 0 "
        "WHEN 'draft' THEN 1 ELSE 2 END, question").fetchall()
    out = [HEAD]
    for qnorm, question, kind, options, choice, cstatus in rows:
        out.append(
            f"ID: {qnorm[:8] if len(qnorm) > 8 else qnorm}\n"
            f"КЛЮЧ: {qnorm}\n"
            f"СТАТУС: {cstatus or 'needs_input'}\n"
            f"ТИП: {kind or '—'}\n"
            f"ВОПРОС: {' '.join(question.split())}\n"
            f"ВАРИАНТЫ: {options}\n"
            f"ВЫБОР: {choice or ''}\n"
            "---")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    print(f"выгружено {len(rows)} в {path}")


def import_(db, path=EDIT_FILE):
    text = open(path, encoding="utf-8").read()
    ok = bad = 0
    for block in text.split("---"):
        fields = {}
        for line in block.splitlines():
            line = line.strip()
            if line.startswith("#") or ":" not in line:
                continue
            k, v = line.split(":", 1)
            if k.strip() in ("КЛЮЧ", "СТАТУС", "ВЫБОР", "ВАРИАНТЫ"):
                fields[k.strip()] = v.strip()
        qnorm = fields.get("КЛЮЧ")
        if not qnorm:
            continue
        status = (fields.get("СТАТУС") or "").split()[0].lower() \
            if fields.get("СТАТУС") else "needs_input"
        choice = fields.get("ВЫБОР", "")
        valid = split_options(fields.get("ВАРИАНТЫ"))
        picked = split_options(choice)
        # одобрять можно только то, что реально есть среди вариантов
        if status == "approved":
            unknown = [p for p in picked if p not in valid]
            if not picked or unknown:
                print(f"  пропущен {qnorm[:8]}: "
                      + ("пустой выбор" if not picked
                         else f"нет таких вариантов: {unknown}"))
                bad += 1
                continue
        db.execute("UPDATE answer_bank SET choice=?, choice_status=? "
                   "WHERE qnorm=?", (choice, status, qnorm))
        ok += 1
    db.commit()
    print(f"обновлено: {ok}, пропущено: {bad}")


def main():
    args = sys.argv[1:]
    db = hh.init_db()
    if "--scan" in args:
        i = args.index("--scan")
        limit = int(args[i + 1]) if len(args) > i + 1 and args[i + 1].isdigit() else None
        scan(db, limit)
    elif "--draft" in args:
        draft(db)
    elif "--export" in args:
        export(db)
    elif "--import" in args:
        i = args.index("--import")
        import_(db, args[i + 1] if len(args) > i + 1 else EDIT_FILE)
    elif "--list" in args:
        show(db)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
