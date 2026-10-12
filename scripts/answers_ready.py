"""Сколько отложенных вакансий готово к отправке на каждом аккаунте."""
import os
import sys

BASE = "/Users/mr_macbook/Documents"
for acc in ("hh_autoreply", "hh_autoreply_2", "hh_autoreply_3"):
    path = f"{BASE}/{acc}"
    os.chdir(path)
    for m in [m for m in sys.modules if m.startswith("hh_")]:
        del sys.modules[m]
    sys.path.insert(0, path)
    import hh_autoapply as hh
    import hh_searchapply as sa
    import hh_choices
    db = hh.init_db()
    ready = sa.answerable_deferred(db)
    _, n = hh_choices.render(db, ("draft", "needs_input"))
    left = db.execute("SELECT COUNT(*) FROM responses WHERE status='questions'").fetchone()[0]
    print(f"{acc:16s} готовы к отправке: {len(ready):2d} | ещё отложено: {left - len(ready):2d} "
          f"| вопросов с выбором без ответа: {n}")
    sys.path.remove(path)
