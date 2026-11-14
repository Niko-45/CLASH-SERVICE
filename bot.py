"""Бот + API (v2). Тағйирёбандаҳо: BOT_TOKEN, ADMIN_CHAT_ID, WEBAPP_URL,
SUPABASE_URL, SUPABASE_SERVICE_KEY (калиди махфӣ, ҳеҷ вақт ба фронтенд надиҳед)."""
import os, re, json, hmac, hashlib, time, asyncio
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import (Message, WebAppInfo, InlineKeyboardMarkup,
                           InlineKeyboardButton, MenuButtonWebApp)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_CHAT_ID"])
WEBAPP_URL = os.environ["WEBAPP_URL"]
SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_SERVICE_KEY"]
PORT = int(os.environ.get("PORT", 8080))

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
HDR = {"apikey": SB_KEY, "Content-Type": "application/json",
       "Prefer": "resolution=merge-duplicates,return=representation"}
if SB_KEY.startswith("eyJ"):
    HDR["Authorization"] = "Bearer " + SB_KEY
IMG = re.compile(r"data:image/(jpeg|png);base64,[A-Za-z0-9+/=]+")


def ok_img(x, limit):
    return isinstance(x, str) and len(x) <= limit and bool(IMG.fullmatch(x))


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
    if not hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), got):
        raise web.HTTPForbidden()
    if time.time() - int(d.get("auth_date", 0)) > 86400:
        raise web.HTTPForbidden()
    return json.loads(d["user"])


async def ctx(req):
    b = await req.json()
    u = auth(b.get("initData", ""))
    return b, u, u["id"] == ADMIN_ID


last_msg = {}  # огоҳии нав ҷои огоҳии пешинаро мегирад


async def tell(uid, text):
    old = last_msg.pop(uid, None)
    if old:
        try:
            await bot.delete_message(uid, old)
        except Exception:
            pass
    try:
        m = await bot.send_message(uid, text)
        last_msg[uid] = m.message_id
    except Exception:
        pass


async def upsert_user(u):
    uid = u["id"]
    rows = await sb("GET", "users", {"id": f"eq.{uid}"})
    if rows:
        if rows[0].get("username") != u.get("username", ""):
            rows = await sb("PATCH", "users", {"id": f"eq.{uid}"}, {"username": u.get("username", "")})
        return rows[0]
    return (await sb("POST", "users", None, {"id": uid, "name": u.get("first_name", ""),
                                             "username": u.get("username", "")}))[0]


async def own_order(b, uid, adm):
    p = {"id": f"eq.{int(b['id'])}"}
    if not adm:
        p["user_id"] = f"eq.{uid}"
    rows = await sb("GET", "orders", p)
    if not rows:
        raise web.HTTPNotFound()
    return rows[0]


routes = web.RouteTableDef()
OK = lambda: web.json_response({"ok": True})


@routes.post("/api/me")
async def me(req):
    b, u, adm = await ctx(req)
    row = await upsert_user(u)
    return web.json_response({"name": row.get("name") or u.get("first_name", ""), "admin": adm})


@routes.post("/api/orders")
async def orders(req):
    b, u, adm = await ctx(req)
    p = {"select": "*,users(name,username)", "order": "id.desc", "limit": "200"}
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
    sv = await sb("GET", "services", {"key": f"eq.{b.get('service')}"})
    if not sv or not sv[0]["active"]:
        raise web.HTTPBadRequest()
    sv = sv[0]
    data = {str(k)[:40]: str(v)[:150] for k, v in list((b.get("data") or {}).items())[:10]}
    note = str(b.get("note", ""))[:800]
    img = b.get("image")
    if img and not ok_img(img, 3_000_000):
        raise web.HTTPBadRequest()
    await upsert_user(u)
    o = (await sb("POST", "orders", None, {"user_id": u["id"], "service": sv["key"], "data": data,
                                           "note": note, "has_image": bool(img)}))[0]
    if img:
        await sb("POST", "order_images", None, {"order_id": o["id"], "data": img})
    await tell(ADMIN_ID, f"📥 Дархости нав #{o['id']} · {sv['name']} · @{u.get('username', '-')}\n"
                         f"Барномаро кушоед.")
    return OK()


@routes.post("/api/orders/image")
async def order_image(req):
    b, u, adm = await ctx(req)
    o = await own_order(b, u["id"], adm)
    rows = await sb("GET", "order_images", {"order_id": f"eq.{o['id']}"})
    return web.json_response({"image": rows[0]["data"] if rows else None})


@routes.post("/api/orders/price")
async def price(req):
    b, u, adm = await ctx(req)
    if not adm:
        raise web.HTTPForbidden()
    o = await own_order(b, u["id"], True)
    pr = int(b["price"])
    if pr <= 0:
        raise web.HTTPBadRequest()
    hours = min(1000.0, max(0.0, float(b.get("hours") or 0)))
    await sb("PATCH", "orders", {"id": f"eq.{o['id']}"},
             {"price": pr, "status": "priced", "hours": hours or None})
    await tell(o["user_id"], f"Нархи фармоиши #{o['id']}: {pr} сомонӣ"
                             + (f", муддат: тақрибан {hours:g} соат" if hours else "")
                             + ". Барномаро кушоед ва тасдиқ кунед.")
    return OK()


@routes.post("/api/orders/confirm")
async def confirm(req):
    b, u, adm = await ctx(req)
    o = await own_order(b, u["id"], False)
    if o["status"] != "priced":
        raise web.HTTPBadRequest()
    if b.get("accept"):
        await sb("PATCH", "orders", {"id": f"eq.{o['id']}"},
                 {"status": "working", "started_at": datetime.now(timezone.utc).isoformat()})
    else:
        await sb("PATCH", "orders", {"id": f"eq.{o['id']}"}, {"status": "rejected"})
    await tell(ADMIN_ID, f"Фармоиши #{o['id']}: клиент {'тасдиқ кард' if b.get('accept') else 'рад кард'}.")
    return OK()


@routes.post("/api/orders/finish")
async def finish(req):
    b, u, adm = await ctx(req)
    if not adm:
        raise web.HTTPForbidden()
    o = await own_order(b, u["id"], True)
    await sb("PATCH", "orders", {"id": f"eq.{o['id']}"},
             {"status": "done", "done_at": datetime.now(timezone.utc).isoformat()})
    await tell(o["user_id"], f"Фармоиши #{o['id']} тайёр шуд!")
    return OK()


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
    await tell(o["user_id"] if adm else ADMIN_ID,
               f"Паём (фармоиши #{o['id']}):\n{text[:200]}\n\nБарномаро кушоед, то ҷавоб диҳед.")
    return OK()


async def broadcast(text):
    for r in await sb("GET", "users", {"select": "id", "limit": "5000"}):
        if r["id"] != ADMIN_ID:
            await tell(r["id"], text)
            await asyncio.sleep(0.05)


@routes.post("/api/ann/list")
async def ann_list(req):
    await ctx(req)
    return web.json_response({"items": await sb("GET", "announcements", {"order": "pinned.desc,id.desc", "limit": "30"})})


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
    return OK()


@routes.post("/api/ann/delete")
async def ann_delete(req):
    b, u, adm = await ctx(req)
    if not adm:
        raise web.HTTPForbidden()
    await sb("DELETE", "announcements", {"id": f"eq.{int(b['id'])}"})
    return OK()


@routes.post("/api/services")
async def services(req):
    await ctx(req)
    return web.json_response({"items": await sb("GET", "services", {"order": "sort.asc,key.asc"})})


@routes.post("/api/services/toggle")
async def services_toggle(req):
    b, u, adm = await ctx(req)
    if not adm:
        raise web.HTTPForbidden()
    await sb("PATCH", "services", {"key": f"eq.{b.get('key')}"}, {"active": bool(b.get("active"))})
    return OK()


@routes.post("/api/services/save")
async def services_save(req):
    b, u, adm = await ctx(req)
    if not adm:
        raise web.HTTPForbidden()
    key = str(b.get("key") or "")[:40]
    exists = bool(key) and bool(await sb("GET", "services", {"key": f"eq.{key}"}))
    if key and not exists:
        raise web.HTTPNotFound()
    name = str(b.get("name", "")).strip()[:40]
    if not name:
        raise web.HTTPBadRequest()
    pr = b.get("price")
    pr = max(0, int(pr)) if pr not in (None, "") else None
    fields = []
    for f in (b.get("fields") or [])[:8]:
        label = str(f.get("label", "")).strip()[:50] if isinstance(f, dict) else ""
        if label:
            fields.append({"label": label, "type": "number" if f.get("type") == "number" else "text",
                           "opt": bool(f.get("opt"))})
    row = {"key": key or "s" + str(int(time.time())), "name": name,
           "icon": str(b.get("icon") or "").strip()[:8] or "🛠️", "price": pr, "fields": fields,
           "acc": bool(b.get("acc")), "active": bool(b.get("active"))}
    if not exists:
        row["sort"] = 100
    img = b.get("image")
    if img:
        if not ok_img(img, 150_000):
            raise web.HTTPBadRequest()
        row["image"] = img
    elif b.get("clear_image"):
        row["image"] = None
    await sb("POST", "services", {"on_conflict": "key"}, row)
    return OK()


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
    return OK()


@routes.post("/api/profile")
async def profile(req):
    b, u, adm = await ctx(req)
    mine = await upsert_user(u)
    master = mine if adm else ((await sb("GET", "users", {"id": f"eq.{ADMIN_ID}"})) or [{}])[0]
    rv = await sb("GET", "reviews", {"select": "rating,text,users(name)", "order": "id.desc", "limit": "5000"})
    done = await sb("GET", "orders", {"select": "id", "status": "eq.done", "limit": "5000"})
    avg = round(sum(r["rating"] for r in rv) / len(rv), 1) if rv else 0
    pick = lambda r: {"name": r.get("name") or "Мутахассис", "avatar": r.get("avatar"), "about": r.get("about") or ""}
    return web.json_response({
        "me": pick(mine),
        "master": {**pick(master), "avg": avg, "count": len(rv), "done": len(done),
                   "reviews": [{"rating": r["rating"], "text": r["text"], "name": (r.get("users") or {}).get("name", "")}
                               for r in rv[:20]]}})


@routes.post("/api/profile/update")
async def profile_update(req):
    b, u, adm = await ctx(req)
    await upsert_user(u)
    patch = {"name": str(b.get("name", "")).strip()[:40] or u.get("first_name", ""),
             "about": str(b.get("about", "")).strip()[:300]}
    av = b.get("avatar")
    if av:
        if not ok_img(av, 100_000):
            raise web.HTTPBadRequest()
        patch["avatar"] = av
    await sb("PATCH", "users", {"id": f"eq.{u['id']}"}, patch)
    return OK()


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
    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
            text="Фармоиш", web_app=WebAppInfo(url=WEBAPP_URL)))
    except Exception:
        pass
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
