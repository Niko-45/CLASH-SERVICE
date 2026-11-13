"""Бот + API. Тағйирёбандаҳо: BOT_TOKEN, ADMIN_CHAT_ID, WEBAPP_URL,
SUPABASE_URL, SUPABASE_SERVICE_KEY (калиди service_role, ҳеҷ вақт ба фронтенд надиҳед)."""
import os, json, hmac, hashlib, time, base64, asyncio
from urllib.parse import parse_qsl
import aiohttp
from datetime import datetime, timedelta, timezone
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import (Message, BufferedInputFile, WebAppInfo,
                           InlineKeyboardMarkup, InlineKeyboardButton)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_CHAT_ID"])
WEBAPP_URL = os.environ["WEBAPP_URL"]
SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_SERVICE_KEY"]
PORT = int(os.environ.get("PORT", 8080))
SERVICES = {"walls": "Деворҳо", "xp": "Сатҳи таҷриба", "clan": "Пур кардани клан",
            "raid": "Рейд ҳамла", "capital": "Пойтахти клан", "custom": "Идеяи худатон"}

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
HDR = {"apikey": SB_KEY, "Content-Type": "application/json",
       "Prefer": "resolution=merge-duplicates,return=representation"}
if SB_KEY.startswith("eyJ"):  # калиди кӯҳнаи service_role (JWT) ба Authorization ҳам лозим аст
    HDR["Authorization"] = "Bearer " + SB_KEY


async def sb(method, table, params=None, body=None):
    async with aiohttp.ClientSession() as s:
        async with s.request(method, f"{SB_URL}/rest/v1/{table}",
                             headers=HDR, params=params, json=body) as r:
            if r.status >= 300:
                raise web.HTTPBadGateway(text=await r.text())
            return await r.json()


def auth(init: str) -> dict:
    d = dict(parse_qsl(init, keep_blank_values=True))
    got = d.pop("hash", None)
    if not got:
        raise web.HTTPForbidden()
    check = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, got):
        raise web.HTTPForbidden()
    if time.time() - int(d.get("auth_date", 0)) > 86400:
        raise web.HTTPForbidden()
    return json.loads(d["user"])


async def ctx(req):
    b = await req.json()
    u = auth(b.get("initData", ""))
    return b, u, u["id"] == ADMIN_ID


async def tell(uid, text):
    try:
        await bot.send_message(uid, text)
    except Exception:
        pass


async def upsert_user(u):
    await sb("POST", "users", {"on_conflict": "id"},
             {"id": u["id"], "name": u.get("first_name", ""), "username": u.get("username", "")})


async def own_order(b, uid, adm):
    p = {"id": f"eq.{int(b['id'])}"}
    if not adm:
        p["user_id"] = f"eq.{uid}"
    rows = await sb("GET", "orders", p)
    if not rows:
        raise web.HTTPNotFound()
    return rows[0]


routes = web.RouteTableDef()


@routes.post("/api/me")
async def me(req):
    b, u, adm = await ctx(req)
    await upsert_user(u)
    return web.json_response({"name": u.get("first_name", ""), "admin": adm})


@routes.post("/api/orders")
async def orders(req):
    b, u, adm = await ctx(req)
    p = {"select": "*,users(name,username)", "order": "id.desc", "limit": "100"}
    if not adm:
        p["user_id"] = f"eq.{u['id']}"
    rows = await sb("GET", "orders", p)
    ids = set()
    if not adm:
        ids = {r["order_id"] for r in await sb("GET", "reviews", {"select": "order_id", "user_id": f"eq.{u['id']}"})}
    for o in rows:
        o["reviewed"] = o["id"] in ids
    return web.json_response({"orders": rows})


@routes.post("/api/orders/create")
async def create(req):
    b, u, adm = await ctx(req)
    sv = b.get("service")
    if sv not in SERVICES:
        raise web.HTTPBadRequest()
    r = await sb("GET", "services", {"key": f"eq.{sv}"})
    if r and not r[0]["active"]:
        raise web.HTTPBadRequest()
    data = {str(k)[:20]: str(v)[:100] for k, v in list((b.get("data") or {}).items())[:10]}
    note = str(b.get("note", ""))[:800]
    await upsert_user(u)
    o = (await sb("POST", "orders", None, {"user_id": u["id"], "service": sv,
                                           "data": data, "note": note}))[0]
    cap = (f"Дархости нав #{o['id']}\n{SERVICES[sv]}\n@{u.get('username', '-')} "
           f"({u.get('first_name', '')})\n" + "\n".join(f"{k}: {v}" for k, v in data.items())
           + (f"\n\n{note}" if note else ""))
    try:
        if b.get("image"):
            img = base64.b64decode(b["image"].split(",", 1)[1])
            await bot.send_photo(ADMIN_ID, BufferedInputFile(img, "s.jpg"), caption=cap[:1000])
        else:
            await bot.send_message(ADMIN_ID, cap)
    except Exception:
        pass
    return web.json_response({"ok": True})


@routes.post("/api/orders/price")
async def price(req):
    b, u, adm = await ctx(req)
    if not adm:
        raise web.HTTPForbidden()
    o = await own_order(b, u["id"], True)
    pr = int(b["price"])
    if pr <= 0:
        raise web.HTTPBadRequest()
    await sb("PATCH", "orders", {"id": f"eq.{o['id']}"}, {"price": pr, "status": "priced"})
    await tell(o["user_id"], f"Нархи фармоиши #{o['id']}: {pr} сомонӣ. "
                             f"Барномаро кушоед ва тасдиқ кунед.")
    return web.json_response({"ok": True})


@routes.post("/api/orders/confirm")
async def confirm(req):
    b, u, adm = await ctx(req)
    o = await own_order(b, u["id"], False)
    if o["status"] != "priced":
        raise web.HTTPBadRequest()
    st = "working" if b.get("accept") else "rejected"
    await sb("PATCH", "orders", {"id": f"eq.{o['id']}"}, {"status": st})
    await tell(ADMIN_ID, f"Фармоиши #{o['id']}: клиент {'тасдиқ кард' if st == 'working' else 'рад кард'}.")
    return web.json_response({"ok": True})


@routes.post("/api/orders/progress")
async def progress(req):
    b, u, adm = await ctx(req)
    if not adm:
        raise web.HTTPForbidden()
    o = await own_order(b, u["id"], True)
    patch = {"progress_done": max(0, int(b.get("done", 0))),
             "progress_total": max(0, int(b.get("total", 0)))}
    if b.get("finish"):
        patch["status"] = "done"
        patch["done_at"] = datetime.now(timezone.utc).isoformat()
    await sb("PATCH", "orders", {"id": f"eq.{o['id']}"}, patch)
    if b.get("finish"):
        await tell(o["user_id"], f"Фармоиши #{o['id']} тайёр шуд!")
    return web.json_response({"ok": True})


@routes.post("/api/chat/list")
async def chat_list(req):
    b, u, adm = await ctx(req)
    o = await own_order(b, u["id"], adm)
    rows = await sb("GET", "messages", {"order_id": f"eq.{o['id']}", "order": "id.asc", "limit": "200"})
    return web.json_response({"messages": [
        {"id": m["id"], "mine": m["sender_id"] == u["id"], "text": m["text"]} for m in rows]})


@routes.post("/api/chat/send")
async def chat_send(req):
    b, u, adm = await ctx(req)
    o = await own_order(b, u["id"], adm)
    text = str(b.get("text", "")).strip()[:1000]
    if not text:
        raise web.HTTPBadRequest()
    await sb("POST", "messages", None, {"order_id": o["id"], "sender_id": u["id"], "text": text})
    to = o["user_id"] if adm else ADMIN_ID
    await tell(to, f"Паём (фармоиши #{o['id']}):\n{text[:200]}\n\nБарномаро кушоед, то ҷавоб диҳед.")
    return web.json_response({"ok": True})


async def broadcast(text):
    users = await sb("GET", "users", {"select": "id", "limit": "5000"})
    for r in users:
        if r["id"] != ADMIN_ID:
            await tell(r["id"], text)
            await asyncio.sleep(0.05)


@routes.post("/api/ann/list")
async def ann_list(req):
    await ctx(req)
    rows = await sb("GET", "announcements", {"order": "pinned.desc,id.desc", "limit": "30"})
    return web.json_response({"items": rows})


@routes.post("/api/ann/create")
async def ann_create(req):
    b, u, adm = await ctx(req)
    if not adm:
        raise web.HTTPForbidden()
    title = str(b.get("title", "")).strip()[:100]
    text = str(b.get("text", "")).strip()[:1000]
    if not title:
        raise web.HTTPBadRequest()
    await sb("POST", "announcements", None, {"title": title, "text": text, "pinned": bool(b.get("pinned"))})
    asyncio.create_task(broadcast(f"📢 {title}\n\n{text}"[:1000]))
    return web.json_response({"ok": True})


@routes.post("/api/ann/delete")
async def ann_delete(req):
    b, u, adm = await ctx(req)
    if not adm:
        raise web.HTTPForbidden()
    await sb("DELETE", "announcements", {"id": f"eq.{int(b['id'])}"})
    return web.json_response({"ok": True})


@routes.post("/api/services")
async def services(req):
    await ctx(req)
    return web.json_response({"items": await sb("GET", "services", {"order": "sort.asc"})})


@routes.post("/api/services/update")
async def services_update(req):
    b, u, adm = await ctx(req)
    if not adm or b.get("key") not in SERVICES:
        raise web.HTTPForbidden()
    pr = b.get("price")
    pr = int(pr) if pr not in (None, "") else None
    await sb("PATCH", "services", {"key": f"eq.{b['key']}"}, {"price": pr, "active": bool(b.get("active"))})
    return web.json_response({"ok": True})


@routes.post("/api/review/create")
async def review_create(req):
    b, u, adm = await ctx(req)
    o = await own_order(b, u["id"], False)
    if o["status"] != "done":
        raise web.HTTPBadRequest()
    rating = min(5, max(1, int(b.get("rating", 5))))
    text = str(b.get("text", "")).strip()[:500]
    await sb("POST", "reviews", None, {"order_id": o["id"], "user_id": u["id"], "rating": rating, "text": text})
    await tell(ADMIN_ID, f"Шарҳи нав барои фармоиши #{o['id']}: {rating}/5\n{text}")
    return web.json_response({"ok": True})


@routes.post("/api/profile")
async def profile(req):
    await ctx(req)
    rv = await sb("GET", "reviews", {"select": "rating,text,users(name)", "order": "id.desc", "limit": "5000"})
    adm = await sb("GET", "users", {"id": f"eq.{ADMIN_ID}"})
    done = await sb("GET", "orders", {"select": "id", "status": "eq.done", "limit": "5000"})
    avg = round(sum(r["rating"] for r in rv) / len(rv), 1) if rv else 0
    return web.json_response({
        "name": adm[0]["name"] if adm else "Мутахассис", "avg": avg, "count": len(rv), "done": len(done),
        "reviews": [{"rating": r["rating"], "text": r["text"], "name": (r.get("users") or {}).get("name", "")}
                    for r in rv[:20]]})


def ts(x):
    return datetime.fromisoformat(x.replace("Z", "+00:00"))


@routes.post("/api/finance")
async def finance(req):
    b, u, adm = await ctx(req)
    if not adm:
        raise web.HTTPForbidden()
    rows = await sb("GET", "orders", {"select": "status,price,created_at,done_at",
                                      "price": "not.is.null", "limit": "5000"})
    now = datetime.now(timezone.utc)
    week = month = total = wait = cnt = 0
    days = [0] * 7
    for r in rows:
        pr = r["price"]
        if r["status"] == "done":
            t = ts(r.get("done_at") or r["created_at"])
            total += pr
            cnt += 1
            if now - t <= timedelta(days=30):
                month += pr
            if now - t <= timedelta(days=7):
                week += pr
            d = (now.date() - t.date()).days
            if 0 <= d < 7:
                days[6 - d] += pr
        elif r["status"] == "working":
            wait += pr
    return web.json_response({"week": week, "month": month, "total": total,
                              "wait": wait, "count": cnt, "days": days})


@web.middleware
async def cors(req, handler):
    try:
        r = web.Response() if req.method == "OPTIONS" else await handler(req)
    except web.HTTPException as e:
        r = e
    r.headers.update({"Access-Control-Allow-Origin": "*",
                      "Access-Control-Allow-Headers": "*",
                      "Access-Control-Allow-Methods": "POST, OPTIONS"})
    return r


@dp.message(CommandStart())
async def start(m: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Кушодани барнома", web_app=WebAppInfo(url=WEBAPP_URL))]])
    await m.answer("Салом! Барои фармоиш тугмаро пахш кунед.", reply_markup=kb)


async def main():
    app = web.Application(middlewares=[cors], client_max_size=15 * 1024 * 1024)
    app.add_routes(routes)
    app.router.add_route("OPTIONS", "/{tail:.*}", lambda r: web.Response())
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
