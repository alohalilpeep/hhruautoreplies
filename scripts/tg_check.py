"""Есть ли у номера аккаунт Telegram — через импорт контакта, как это делает человек.

    python scripts/tg_check.py --login                    один раз: вход в аккаунт (код из Telegram)
    python scripts/tg_check.py --phone "+7 903 000-00-00" [--company "ООО Ромашка"]
    python scripts/tg_check.py                            все номера из tg/phones.csv

Номер добавляется в контакты, из ответа берутся имя, фамилия и ник, контакт сразу
удаляется. Если человек закрыл поиск по номеру в настройках приватности, аккаунт
не найдётся, даже если он есть.

Ограничения: Telegram не любит массовый импорт контактов (FloodWait, затем бан),
поэтому не больше TG_DAILY_LIMIT номеров в сутки (по умолчанию 50) и пауза 20–45 с
между номерами. На FloodWait скрипт останавливается. Аккаунт — отдельный, не основной
и не свежезарегистрированный. Писать людям лучше руками: массовые холодные сообщения
быстро ловят жалобы на спам.

Ключи в .env первого аккаунта: TG_API_ID, TG_API_HASH (my.telegram.org), TG_PHONE —
номер аккаунта, с которого проверяем. Вход, номера и результаты — в папке tg/ (в .gitignore).
Вход: tg/phones.csv (колонки company, phone). Выход: tg/tg_result.csv, дописывается;
уже проверенные номера повторно не дёргаем.
"""
import asyncio
import csv
import datetime as dt
import json
import random
import re
import sys
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path("/Users/mr_macbook/Documents/hh_autoreply")
TG = ROOT / "tg"
PHONES = TG / "phones.csv"
RESULT = TG / "tg_result.csv"
STATE = TG / "tg_state.json"                       # сколько номеров проверено за сутки
SESSION = TG / "tg_check"                          # telethon допишет .session
FIELDS = ["checked_at", "company", "phone", "found", "first_name", "last_name",
          "username", "user_id", "note"]


def norm_phone(s):
    """«+7 (903) 082-60-68», «89030826068» → «+79030826068»."""
    d = re.sub(r"\D", "", s or "")
    if len(d) == 11 and d[0] == "8":
        d = "7" + d[1:]
    if len(d) == 10:
        d = "7" + d
    return "+" + d if d else ""


def env():
    e = dotenv_values(ROOT / ".env")
    if not e.get("TG_API_ID") or not e.get("TG_API_HASH"):
        raise SystemExit("нет TG_API_ID / TG_API_HASH в .env первого аккаунта (берутся на my.telegram.org)")
    return int(e["TG_API_ID"]), e["TG_API_HASH"], e.get("TG_PHONE", ""), \
        int(e.get("TG_DAILY_LIMIT") or 50)


def load_state():
    today = dt.date.today().isoformat()
    try:
        s = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        s = {}
    return s if s.get("date") == today else {"date": today, "count": 0}


def checked_phones():
    if not RESULT.exists():
        return set()
    with RESULT.open(encoding="utf-8") as f:
        return {r["phone"] for r in csv.DictReader(f)}


def write_result(row):
    new = not RESULT.exists()
    with RESULT.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)


async def check_one(client, phone, company):
    from telethon.tl.functions.contacts import DeleteContactsRequest, ImportContactsRequest
    from telethon.tl.types import InputPhoneContact
    res = await client(ImportContactsRequest([InputPhoneContact(
        client_id=random.randrange(1, 2 ** 31), phone=phone, first_name=company[:60] or "check",
        last_name="")]))
    row = {"checked_at": dt.datetime.now().isoformat(timespec="seconds"), "company": company,
           "phone": phone, "found": "нет", "first_name": "", "last_name": "", "username": "",
           "user_id": "", "note": ""}
    if res.users:
        u = res.users[0]
        row.update({"found": "да", "first_name": u.first_name or "", "last_name": u.last_name or "",
                    "username": ("@" + u.username) if u.username else "", "user_id": u.id})
        await client(DeleteContactsRequest(id=[u]))          # контакт сразу убираем
    elif res.retry_contacts:
        row["note"] = "Telegram попросил повторить позже"
    return row


async def run(todo, limit):
    from telethon import TelegramClient
    from telethon.errors import FloodWaitError
    api_id, api_hash, my_phone, _ = env()
    TG.mkdir(exist_ok=True)
    async with TelegramClient(str(SESSION), api_id, api_hash) as client:
        if not await client.is_user_authorized():
            raise SystemExit("нет входа: сначала python scripts/tg_check.py --login")
        state = load_state()
        done = 0
        for i, (company, phone) in enumerate(todo):
            if state["count"] >= limit:
                print(f"суточный лимит {limit} номеров исчерпан — продолжу завтра")
                break
            try:
                row = await check_one(client, phone, company)
            except FloodWaitError as e:
                print(f"FloodWait: Telegram просит подождать {e.seconds} с — останавливаюсь")
                break
            write_result(row)
            state["count"] += 1
            STATE.write_text(json.dumps(state), encoding="utf-8")
            done += 1
            who = " ".join(x for x in (row["first_name"], row["last_name"], row["username"]) if x)
            print(f"{phone} | {company or '—'} | {row['found']} | {who or row['note'] or '—'}", flush=True)
            if i < len(todo) - 1 and state["count"] < limit:
                await asyncio.sleep(random.uniform(20, 45))
        print(f"проверено сейчас: {done}, за сегодня всего: {state['count']} из {limit}. "
              f"результат: {RESULT}")


async def login():
    from telethon import TelegramClient
    api_id, api_hash, my_phone, _ = env()
    TG.mkdir(exist_ok=True)
    client = TelegramClient(str(SESSION), api_id, api_hash)
    await client.start(phone=my_phone or None)     # спросит код из Telegram (и пароль 2FA)
    me = await client.get_me()
    print(f"вход выполнен: {me.first_name or ''} {('@' + me.username) if me.username else ''}")
    await client.disconnect()


def main():
    a = sys.argv[1:]
    if "--login" in a:
        asyncio.run(login())
        return
    _, _, _, limit = env()
    seen = checked_phones()
    if "--phone" in a:
        phone = norm_phone(a[a.index("--phone") + 1])
        company = a[a.index("--company") + 1] if "--company" in a else ""
        todo = [(company, phone)]
    else:
        if not PHONES.exists():
            raise SystemExit(f"нет {PHONES} (колонки company, phone)")
        with PHONES.open(encoding="utf-8") as f:
            todo = [(r.get("company", ""), norm_phone(r.get("phone", ""))) for r in csv.DictReader(f)]
        todo = [(c, p) for c, p in todo if p and p not in seen]
    print(f"номеров к проверке: {len(todo)}")
    asyncio.run(run(todo, limit))


if __name__ == "__main__":
    main()
