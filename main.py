import os, asyncio
from datetime import datetime, timedelta, timezone

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

from fastapi import FastAPI
import uvicorn

TOKEN = os.environ["BOT_TOKEN"]
PORT = int(os.environ.get("PORT", "10000"))  # Render дає PORT
API_URL = "https://api-toe-poweron.inneti.net/api/actual_gpv_graphs"
PAGE_SIZE = 20
POLL_SECONDS = 300

users = {}  # chat_id -> {"group": str|None, "last_slot": str|None, "last_status": str|None}
_groups_cache = {"ts": None, "groups": []}

def is_default_group(g: str) -> bool:
    return "#" not in g

def fmt_status(s: str) -> str:
    return {"0": "Світло є ✅", "10": "Жовта зона 🟡", "1": "Відключення 🔌"}.get(s, f"Невідомо({s})")

def build_params(now_utc: datetime) -> dict:
    after = (now_utc - timedelta(days=2)).replace(microsecond=0)
    before = (now_utc + timedelta(days=2)).replace(microsecond=0)
    return {
        "dateGraph[before]": before.isoformat().replace("+00:00", "Z"),
        "dateGraph[after]": after.isoformat().replace("+00:00", "Z"),
    }

async def fetch_json(session: aiohttp.ClientSession) -> dict:
    now_utc = datetime.now(timezone.utc)
    async with session.get(API_URL, params=build_params(now_utc), timeout=20) as r:
        r.raise_for_status()
        return await r.json()

async def get_groups(session: aiohttp.ClientSession, cache_minutes: int = 60) -> list[str]:
    ts = _groups_cache["ts"]
    if ts and (datetime.now(timezone.utc) - ts) < timedelta(minutes=cache_minutes):
        return _groups_cache["groups"]
    data = await fetch_json(session)
    groups = sorted([g for g in data.get("dataJson", {}) if is_default_group(g)])
    _groups_cache["ts"] = datetime.now(timezone.utc)
    _groups_cache["groups"] = groups
    return groups

def groups_kb(groups: list[str], page: int):
    kb = InlineKeyboardBuilder()
    start = page * PAGE_SIZE
    for g in groups[start:start + PAGE_SIZE]:
        kb.button(text=g, callback_data=f"set:{g}")
    kb.adjust(4)
    nav = InlineKeyboardBuilder()
    if page > 0: nav.button(text="⬅️", callback_data=f"page:{page-1}")
    if start + PAGE_SIZE < len(groups): nav.button(text="➡️", callback_data=f"page:{page+1}")
    if nav.buttons: kb.row(*nav.buttons)
    return kb.as_markup()

def resolve_slot(times: dict[str, str], now_local: datetime):
    slots = []
    for hhmm, st in times.items():
        h, m = map(int, hhmm.split(":"))
        dt = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
        slots.append((dt, hhmm, st))
    slots.sort(key=lambda x: x[0])

    cur = None
    nxt = None
    for i, (dt, hhmm, st) in enumerate(slots):
        if dt <= now_local:
            cur = (hhmm, st)
            if i + 1 < len(slots): nxt = (slots[i + 1][1], slots[i + 1][2])
        else:
            if cur is None:
                cur = (slots[-1][1], slots[-1][2])
                nxt = (slots[0][1], slots[0][2])
            break
    if cur is None:
        cur = (slots[-1][1], slots[-1][2])
        nxt = (slots[0][1], slots[0][2])
    return cur, nxt

async def fetch_times(session: aiohttp.ClientSession, group: str) -> dict[str, str]:
    data = await fetch_json(session)
    return data["dataJson"][group]["times"]

def status_text(group: str, cur, nxt) -> str:
    msg = [f"Група: {group}", f"Зараз ({cur[0]}) - {fmt_status(cur[1])}"]
    if nxt: msg.append(f"Далі ({nxt[0]}) - {fmt_status(nxt[1])}")
    return "\n".join(msg)

async def poller(bot: Bot):
    async with aiohttp.ClientSession() as session:
        while True:
            now_local = datetime.now()
            for chat_id, u in list(users.items()):
                g = u.get("group")
                if not g: continue
                try:
                    times = await fetch_times(session, g)
                    cur, nxt = resolve_slot(times, now_local)
                    if u.get("last_slot") != cur[0] or u.get("last_status") != cur[1]:
                        await bot.send_message(chat_id, status_text(g, cur, nxt))
                        u["last_slot"], u["last_status"] = cur[0], cur[1]
                except Exception:
                    pass
            await asyncio.sleep(POLL_SECONDS)

def build_dp():
    dp = Dispatcher()

    @dp.message(Command("start"))
    async def start(m: Message):
        users.setdefault(m.chat.id, {"group": None, "last_slot": None, "last_status": None})
        async with aiohttp.ClientSession() as session:
            groups = await get_groups(session)
        await m.answer("Обери свою групу:", reply_markup=groups_kb(groups, 0))

    @dp.callback_query(F.data.startswith("page:"))
    async def page(cb: CallbackQuery):
        page = int(cb.data.split(":")[1])
        async with aiohttp.ClientSession() as session:
            groups = await get_groups(session)
        await cb.message.edit_reply_markup(reply_markup=groups_kb(groups, page))
        await cb.answer()

    @dp.callback_query(F.data.startswith("set:"))
    async def set_group(cb: CallbackQuery):
        g = cb.data.split(":")[1]
        users.setdefault(cb.message.chat.id, {"group": None, "last_slot": None, "last_status": None})
        users[cb.message.chat.id].update({"group": g, "last_slot": None, "last_status": None})
        await cb.message.edit_text(f"Збережено ✅ Група: {g}\n/status - перевірити зараз")
        await cb.answer("OK")

    @dp.message(Command("status"))
    async def status(m: Message):
        u = users.get(m.chat.id)
        if not u or not u.get("group"):
            await m.answer("Спочатку обери групу через /start")
            return
        g = u["group"]
        async with aiohttp.ClientSession() as session:
            times = await fetch_times(session, g)
        cur, nxt = resolve_slot(times, datetime.now())
        await m.answer(status_text(g, cur, nxt))

    return dp

app = FastAPI()

@app.get("/")
def ok():
    return {"ok": True}

@app.on_event("startup")
async def startup():
    bot = Bot(TOKEN)
    dp = build_dp()
    asyncio.create_task(poller(bot))
    asyncio.create_task(dp.start_polling(bot))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
