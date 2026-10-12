"""Перенести одобренные ответы между тремя аккаунтами.

Каждый аккаунт получает то, что одобрено на других, если у него этот
вопрос ещё не одобрен и не помечен skip. Телеграм и телефон аккаунта-
источника подменяются контактами получателя: у аккаунтов они разные, и
без подмены второй рассылал бы работодателям ник первого.
"""
import re
import sqlite3
import datetime as dt

BASE = "/Users/mr_macbook/Documents"
ACCS = ("hh_autoreply", "hh_autoreply_2", "hh_autoreply_3")
TS = dt.datetime.now().isoformat(timespec="seconds")


def contacts(acc):
    prof = {}
    for line in open(f"{BASE}/{acc}/profile.txt", encoding="utf-8"):
        if ":" in line and not line.startswith("#"):
            k, v = line.split(":", 1)
            prof[k.strip().lower()] = v.strip()
    return prof.get("телеграм"), prof.get("телефон")


def localize(text, src, dst):
    """Телеграм и телефон источника → контакты получателя."""
    (tg_s, ph_s), (tg_d, ph_d) = contacts(src), contacts(dst)
    if text and tg_s and tg_d:
        text = re.sub(re.escape(tg_s) + r"(?!\w)", tg_d, text)
    ds = re.sub(r"\D", "", ph_s or "")[-10:]
    dd = re.sub(r"\D", "", ph_d or "")[-10:]
    if text and len(ds) == 10 and ph_d and ds != dd:
        rx = r"(?:\+7|8)?[\s\-()]*" + r"[\s\-()]*".join(ds) + r"(?!\d)"
        text = re.sub(rx, ph_d, text)
    return text


def merge(src_name, dst_name):
    src = sqlite3.connect(f"{BASE}/{src_name}/hh_responses.db")
    dst = sqlite3.connect(f"{BASE}/{dst_name}/hh_responses.db", timeout=30)
    dst.execute("PRAGMA busy_timeout=30000")
    have = dict(dst.execute("SELECT qnorm, status FROM answer_bank"))
    n = 0
    for qn, q, ans, kind, opts, choice, cst in src.execute(
            "SELECT qnorm, question, answer, kind, options, choice, choice_status "
            "FROM answer_bank WHERE status='approved' AND trim(coalesce(answer,''))!=''"):
        cur = have.get(qn)
        if cur in ("approved", "skip"):
            continue
        ans = localize(ans, src_name, dst_name)
        if cur is None:
            dst.execute("INSERT INTO answer_bank (qnorm, question, answer, status, ts,"
                        " kind, options, choice, choice_status) VALUES (?,?,?,?,?,?,?,?,?)",
                        (qn, q, ans, "approved", TS, kind, opts,
                         choice if cst == "approved" else None,
                         "approved" if cst == "approved" else None))
        else:
            dst.execute("UPDATE answer_bank SET answer=?, status='approved', ts=?, "
                        "kind=COALESCE(kind,?), options=COALESCE(options,?) WHERE qnorm=?",
                        (ans, TS, kind, opts, qn))
        n += 1
    for qn, choice, own in src.execute(
            "SELECT qnorm, choice, choice_text FROM answer_bank "
            "WHERE choice_status='approved' AND trim(coalesce(choice,''))!=''"):
        r = dst.execute("SELECT choice_status FROM answer_bank WHERE qnorm=?", (qn,)).fetchone()
        if r and r[0] not in ("approved", "skip"):
            dst.execute("UPDATE answer_bank SET choice=?, choice_status='approved', "
                        "choice_text=? WHERE qnorm=?",
                        (choice, localize(own, src_name, dst_name) if own else None, qn))
            n += 1
    dst.commit()
    return n


if __name__ == "__main__":
    for dst in ACCS:
        got = sum(merge(src, dst) for src in ACCS if src != dst)
        print(f"{dst:18s} получил ответов: {got}")
