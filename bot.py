"""Бот + API (v4). Тағйирёбандаҳо: BOT_TOKEN, ADMIN_CHAT_ID, WEBAPP_URL,
SUPABASE_URL, SUPABASE_SERVICE_KEY (калиди махфӣ, ҳеҷ вақт ба фронтенд надиҳед)."""
import os, re, io, csv, json, hmac, hashlib, time, base64, asyncio
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart, Command
from aiogram.types import (Message, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton,
                           MenuButtonWebApp, BufferedInputFile, InputMediaPhoto)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_CHAT_ID"])
WEBAPP_URL = os.environ["WEBAPP_URL"]
SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_SERVICE_KEY"]
PORT = int(os.environ.get("PORT", 8080))
BOT_NAME = ""
DEBUG = os.environ.get("DEBUG") == "1"  # дар Railway DEBUG=1 гузоред, агар матни пурраи хатоҳоро хоҳед
BANNED = set()
TZ_OFFSET_H = 5  # Душанбе = UTC+5
LOCKS = {}
_hits = {}

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
HDR = {"apikey": SB_KEY, "Content-Type": "application/json",
       "Prefer": "resolution=merge-duplicates,return=representation"}
if SB_KEY.startswith("eyJ"):
    HDR["Authorization"] = "Bearer " + SB_KEY
IMG = re.compile(r"data:image/(jpeg|png);base64,[A-Za-z0-9+/=]+")
OPEN = "(new,priced,unpaid,checking,working)"
SETTING_KEYS = ("pay_info", "rules", "busy", "max_open", "hours", "away", "channel", "storage_chat")
AWAY = ("Ҳоло берун аз соатҳои кориям. Дар соатҳои корӣ ҷавоб медиҳам. / "
        "Сейчас нерабочее время. Отвечу в рабочие часы.")


def ok_img(x, limit):
    return isinstance(x, str) and len(x) <= limit and bool(IMG.fullmatch(x))


SESSION = None  # як пайвасти доимӣ ба база (суръати зиёдтар)
_c = {}


def cache_get(k, ttl):
    v = _c.get(k)
    return v[1] if v and time.time() - v[0] < ttl else None


def cache_set(k, val):
    _c[k] = (time.time(), val)
    return val


async def _nop():
    return []


async def sb(method, table, params=None, body=None):
    async with SESSION.request(method, f"{SB_URL}/rest/v1/{table}",
                               headers=HDR, params=params, json=body) as r:
        if r.status >= 300:
            err = await r.text()
            print("sb error", r.status, table, err[:300])
            raise web.HTTPBadGateway(text=err if DEBUG else "db error")
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
    age = time.time() - int(d.get("auth_date", 0))
    if age > 86400 or age < -300:
        raise web.HTTPForbidden()
    return json.loads(d["user"])


def hit(uid, key, limit, window):
    """Маҳдудияти суръат: аз спам муҳофизат мекунад."""
    now = time.time()
    q = [t for t in _hits.get((uid, key), []) if now - t < window]
    if len(q) >= limit:
        _hits[(uid, key)] = q
        raise web.HTTPTooManyRequests(text="rate")
    q.append(now)
    _hits[(uid, key)] = q


async def ctx(req):
    b = await req.json()
    u = auth(b.get("initData", ""))
    adm = u["id"] == ADMIN_ID
    if not adm:
        if u["id"] in BANNED:
            raise web.HTTPForbidden(text="banned")
        hit(u["id"], "all", 240, 60)
    return b, u, adm


def ts(x):
    return datetime.fromisoformat(x.replace("Z", "+00:00"))


def in_hours(spec):
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*", spec or "")
    if not m:
        return True
    a, b = int(m[1]) * 60 + int(m[2]), int(m[3]) * 60 + int(m[4])
    n = datetime.now(timezone.utc) + timedelta(hours=TZ_OFFSET_H)
    c = n.hour * 60 + n.minute
    return a <= c < b if a < b else (c >= a or c < b)


def user_lock(uid):
    return LOCKS.setdefault(uid, asyncio.Lock())


async def audit(actor, action, oid=None, info=""):
    try:
        await sb("POST", "audit_log", None, {"actor": actor, "action": action, "order_id": oid, "info": str(info)[:200]})
    except Exception as e:
        print("audit:", e)


async def use_promo(code, uid):
    """Промокодро бе хатои ҳамзамонӣ истифода мебарад (compare-and-set дар база)."""
    for _ in range(4):
        pr = await sb("GET", "promos", {"code": f"eq.{code}"})
        if not pr or pr[0]["uses_left"] <= 0:
            return False
        done = await sb("PATCH", "promos", {"code": f"eq.{code}", "uses_left": f"eq.{pr[0]['uses_left']}"},
                        {"uses_left": pr[0]["uses_left"] - 1})
        if done:
            await sb("POST", "promo_uses", None, {"code": code, "user_id": uid})
            return True
    return False


M = {
    "tg": {
        "new": "📥 Дархости нав #{id} · {name} · @{user}\nБарномаро кушоед.",
        "price": "Нархи фармоиши #{id}: {price} сомонӣ{hours}. Барномаро кушоед ва тасдиқ кунед.",
        "hours": ", муддат: тақрибан {h} соат",
        "accepted": "Фармоиши #{id}: клиент нархро қабул кард. Интизори пардохт.",
        "declined": "Фармоиши #{id}: клиент нархро рад кард.",
        "receipt": "💳 Фармоиши #{id}: квитансия омад. Санҷед.",
        "paid_ok": "✅ Пардохт қабул шуд. Кор оғоз шуд! (фармоиши #{id})",
        "paid_ok_acc": "✅ Пардохт қабул шуд. Кор оғоз шуд! (фармоиши #{id})\nДар вақти кор ба бозӣ аз ин аккаунт надароед, то кор халал нанамояд.",
        "loyalty": "🎁 Барои шумо тахфифи доимии {pct}% танзим шуд!",
        "paid_bad": "❌ Квитансияи фармоиши #{id} қабул нашуд. Дубора фиристед ё дар чат нависед.",
        "done": "Фармоиши #{id} тайёр шуд!",
        "cancel_admin": "Фармоиши #{id} бекор шуд.\nСабаб: {reason}",
        "cancel_client": "Фармоиши #{id}: клиент бекор кард.",
        "cancel_paid": "Фармоиши #{id}: клиент баъди фиристодани квитансия бекор кард. Агар пул гирифта бошед, баргардонед.",
        "chat": "Паём (фармоиши #{id}):\n{text}\n\nБарномаро кушоед, то ҷавоб диҳед.",
        "review": "Шарҳи нав барои фармоиши #{id}: {rating}/5\n{text}",
        "remind_priced": "⏰ Фармоиши #{id}: нарх интизори тасдиқи шумост. Барномаро кушоед.",
        "remind_unpaid": "⏰ Фармоиши #{id}: пардохт интизор аст. Квитансияро бор кунед.",
        "auto_cancel": "Фармоиши #{id} бекор шуд, чунки ҷавоб нарасид. Метавонед фармоиши нав диҳед.",
        "ref_reward": "🎁 Дӯсти шумо аввалин фармоишро анҷом дод. Шумо 10% тахфиф гирифтед!",
    },
    "ru": {
        "new": "📥 Новый заказ #{id} · {name} · @{user}\nОткройте приложение.",
        "price": "Цена заказа #{id}: {price} сомони{hours}. Откройте приложение и подтвердите.",
        "hours": ", срок: около {h} ч",
        "accepted": "Заказ #{id}: клиент принял цену. Ждём оплату.",
        "declined": "Заказ #{id}: клиент отклонил цену.",
        "receipt": "💳 Заказ #{id}: пришла квитанция. Проверьте.",
        "paid_ok": "✅ Оплата принята. Работа началась! (заказ #{id})",
        "paid_ok_acc": "✅ Оплата принята. Работа началась! (заказ #{id})\nНе заходите в игру с этого аккаунта, пока идёт работа.",
        "loyalty": "🎁 Для вас установлена постоянная скидка {pct}%!",
        "paid_bad": "❌ Квитанция заказа #{id} не подошла. Отправьте снова или напишите в чат.",
        "done": "Заказ #{id} готов!",
        "cancel_admin": "Заказ #{id} отменён.\nПричина: {reason}",
        "cancel_client": "Заказ #{id}: клиент отменил.",
        "cancel_paid": "Заказ #{id}: клиент отменил после отправки квитанции. Если оплата получена, верните её.",
        "chat": "Сообщение (заказ #{id}):\n{text}\n\nОткройте приложение, чтобы ответить.",
        "review": "Новый отзыв к заказу #{id}: {rating}/5\n{text}",
        "remind_priced": "⏰ Заказ #{id}: цена ждёт вашего подтверждения. Откройте приложение.",
        "remind_unpaid": "⏰ Заказ #{id}: ждём оплату. Загрузите квитанцию.",
        "auto_cancel": "Заказ #{id} отменён, так как не было ответа. Можно сделать новый заказ.",
        "ref_reward": "🎁 Ваш друг выполнил первый заказ. Вам скидка 10%!",
    },
}
LANG = {}
last_msg = {}


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


async def lang_of(uid):
    if uid not in LANG:
        r = await sb("GET", "users", {"id": f"eq.{uid}", "select": "lang"})
        LANG[uid] = (r[0].get("lang") if r else None) or "tg"
    return LANG[uid]


async def tellk(uid, key, **kw):
    lg = await lang_of(uid)
    await tell(uid, M.get(lg, M["tg"])[key].format(**kw))


def nm(name, lg):
    p = str(name).split(" / ")
    return (p[1] if lg == "ru" and len(p) > 1 else p[0]).strip()


def estimate(sv, data):
    unit = sv.get("unit_price")
    if not unit:
        return None
    fr = to = cnt = None
    for f in sv.get("fields") or []:
        try:
            n = int(float(data.get(f.get("label"), "")))
        except (TypeError, ValueError):
            n = None
        r = f.get("role")
        if r == "from":
            fr = n
        elif r == "to":
            to = n
        elif r == "count":
            cnt = n
    if fr is not None and to is not None:
        return min(1_000_000, max(0, to - fr) * unit)
    if cnt is not None:
        return min(1_000_000, max(0, cnt) * unit)
    return None


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


async def get_settings():
    v = cache_get("set", 5)
    return v if v is not None else cache_set("set", {r["key"]: r["value"] for r in await sb("GET", "settings")})


async def get_services():
    v = cache_get("svc", 20)
    return v if v is not None else cache_set("svc", await sb("GET", "services", {"order": "sort.asc,key.asc"}))


STORE_KINDS = {"result": "Натиҷа", "receipt": "Чек"}
IMGCACHE = {}


async def store_chat():
    v = ((await get_settings()).get("storage_chat") or "").strip()
    return int(v) if re.fullmatch(r"-?\d+", v) else None


async def topic_for(kind):
    st = await get_settings()
    v = (st.get("tp_" + kind) or "").strip()
    if v == "0":
        return None  # гурӯҳ бе мавзӯъ (ё канал): танҳо ҳештег
    if v.isdigit():
        return int(v)
    tid = 0
    try:
        tid = (await bot.create_forum_topic(await store_chat(), STORE_KINDS[kind])).message_thread_id
    except Exception as e:
        print("create_forum_topic:", e)
    await sb("POST", "settings", {"on_conflict": "key"}, [{"key": "tp_" + kind, "value": str(tid)}])
    _c.clear()
    return tid or None


def media_src(row, col="data"):
    if row.get("file_id"):
        return row["file_id"]
    return BufferedInputFile(base64.b64decode(row[col].split(",", 1)[1]), "p.jpg")


async def archive_order(oid):
    """Фармоиши муваффақ: до/после + шарҳ дар як паём, чек алоҳида. Баъд расмҳо аз база тоза мешаванд."""
    cid = await store_chat()
    if not cid:
        return False
    try:
        rows = await sb("GET", "orders", {"id": f"eq.{oid}", "select": "*,users(name,username)"})
        if not rows or rows[0]["status"] != "done" or rows[0].get("archived"):
            return False
        o = rows[0]
        rv, before, files = await asyncio.gather(
            sb("GET", "reviews", {"order_id": f"eq.{oid}"}),
            sb("GET", "order_images", {"order_id": f"eq.{oid}"}),
            sb("GET", "order_files", {"order_id": f"eq.{oid}", "order": "id.asc"}))
        after = [f for f in files if f["kind"] == "result"][:8]
        receipts = [f for f in files if f["kind"] == "receipt"]
        sv = next((x for x in await get_services() if x["key"] == o["service"]), None)
        usr = o.get("users") or {}
        who = "@" + str(usr.get("username") or usr.get("name") or "-")
        cap = f"✅ {sv['name'] if sv else o['service']}\n#заказ{oid} · {who}"
        if o.get("price"):
            cap += f" · {o['price']} сомонӣ"
        lbl, k = [], 1
        if before:
            lbl.append("До: 1")
            k = 2
        if after:
            lbl.append(f"После: {k}" + (f"–{k + len(after) - 1}" if len(after) > 1 else ""))
        if lbl:
            cap += "\n📷 " + " · ".join(lbl)
        if rv:
            cap += f"\n⭐ {rv[0]['rating']}/5" + (f" «{rv[0]['text']}»" if rv[0]["text"] else "")
        cap = cap[:1000]
        items = [("order_images", "order_id", r) for r in before[:1]] + [("order_files", "id", r) for r in after]
        tid = await topic_for("result")
        if not items:
            msgs = [await bot.send_message(cid, cap, message_thread_id=tid)]
        elif len(items) == 1:
            msgs = [await bot.send_photo(cid, media_src(items[0][2]), caption=cap, message_thread_id=tid)]
        else:
            msgs = await bot.send_media_group(
                cid, [InputMediaPhoto(media=media_src(r), caption=cap if n == 0 else None)
                      for n, (_, _, r) in enumerate(items)], message_thread_id=tid)
        for (table, pk, r), m in zip(items, msgs):
            if m.photo and not r.get("file_id"):
                await sb("PATCH", table, {pk: f"eq.{r[pk]}"}, {"file_id": m.photo[-1].file_id, "data": None})
        if receipts:
            last = receipts[-1]
            m = await bot.send_photo(
                cid, media_src(last), message_thread_id=await topic_for("receipt"),
                caption=f"#Чек #заказ{oid} · {who}" + (f" · {o['price']} сомонӣ" if o.get("price") else ""))
            if not last.get("file_id"):
                await sb("PATCH", "order_files", {"id": f"eq.{last['id']}"},
                         {"file_id": m.photo[-1].file_id, "data": None})
            for r in receipts[:-1]:
                await sb("DELETE", "order_files", {"id": f"eq.{r['id']}"})
        await sb("PATCH", "messages", {"order_id": f"eq.{oid}", "has_img": "eq.true"},
                 {"image": None, "has_img": False, "file_id": None})
        await sb("PATCH", "orders", {"id": f"eq.{oid}"}, {"archived": True, "arch_msg": msgs[0].message_id})
        return True
    except Exception as e:
        print("archive_order:", e)
        return False


async def late_review(o, rating, text):
    cid = await store_chat()
    if not cid or not o.get("arch_msg"):
        return
    try:
        await bot.send_message(cid, f"⭐ {rating}/5" + (f" «{text}»" if text else ""),
                               reply_to_message_id=o["arch_msg"], message_thread_id=await topic_for("result"))
    except Exception as e:
        print("late_review:", e)


async def purge_media(oid):
    await sb("DELETE", "order_images", {"order_id": f"eq.{oid}"})
    await sb("DELETE", "order_files", {"order_id": f"eq.{oid}"})
    await sb("PATCH", "messages", {"order_id": f"eq.{oid}", "has_img": "eq.true"},
             {"image": None, "has_img": False, "file_id": None})
    await sb("PATCH", "orders", {"id": f"eq.{oid}"}, {"has_image": False, "purged": True})


async def fetch_image(file_id):
    if file_id in IMGCACHE:
        return IMGCACHE[file_id]
    f = await bot.get_file(file_id)
    buf = io.BytesIO()
    await bot.download_file(f.file_path, destination=buf)
    url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    if len(IMGCACHE) >= 40:
        IMGCACHE.pop(next(iter(IMGCACHE)))
    IMGCACHE[file_id] = url
    return url


async def img_of(row, col="data"):
    if row.get("file_id"):
        try:
            return await fetch_image(row["file_id"])
        except Exception as e:
            print("fetch_image:", e)
            return None
    return row.get(col)


routes = web.RouteTableDef()
OK = lambda: web.json_response({"ok": True})


@routes.get("/health")
async def health(req):
    try:
        await sb("GET", "settings", {"key": "eq.busy", "select": "key"})
        return web.Response(text="ok")
    except Exception:
        return web.Response(status=500, text="db")


@routes.post("/api/me")
async def me(req):
    b, u, adm = await ctx(req)
    row = await upsert_user(u)
    lg = b.get("lang")
    if lg in ("tg", "ru"):
        LANG[u["id"]] = lg
        if row.get("lang") != lg:
            await sb("PATCH", "users", {"id": f"eq.{u['id']}"}, {"lang": lg})
    return web.json_response({"id": u["id"], "name": row.get("name") or u.get("first_name", ""), "admin": adm,
                              "tag": row.get("tag") or "", "clan_tag": row.get("clan_tag") or "",
                              "discount": row.get("discount") or 0, "loyalty": row.get("loyalty_pct") or 0, "bot": BOT_NAME})


@routes.post("/api/orders")
async def orders(req):
    b, u, adm = await ctx(req)
    p = {"select": "*,users(name,username,banned,loyalty_pct)", "order": "id.desc", "limit": "500"}
    if not adm:
        p["user_id"] = f"eq.{u['id']}"
    rows = await sb("GET", "orders", p)
    rv_t = _nop() if adm else sb("GET", "reviews", {"select": "order_id", "user_id": f"eq.{u['id']}"})
    ms_t = sb("GET", "messages", {"select": "order_id,id,sender_id",
                                  "order_id": "in.(" + ",".join(str(o["id"]) for o in rows) + ")",
                                  "limit": "20000"}) if rows else _nop()
    dn_t = sb("GET", "orders", {"select": "user_id,price", "status": "eq.done", "limit": "20000"}) if adm else _nop()
    rv, ms, dn = await asyncio.gather(rv_t, ms_t, dn_t)
    stats = {}
    for r in dn:
        x = stats.setdefault(str(r["user_id"]), {"done": 0, "sum": 0})
        x["done"] += 1
        x["sum"] += r["price"] or 0
    ids = {r["order_id"] for r in rv}
    by = {}
    for m in ms:
        by.setdefault(m["order_id"], []).append(m)
    for o in rows:
        o["reviewed"] = o["id"] in ids
        read = (o.get("admin_read_id") if adm else o.get("client_read_id")) or 0
        o["unread"] = sum(1 for m in by.get(o["id"], [])
                          if m["id"] > read and m["sender_id"] not in (u["id"], 0))
    return web.json_response({"orders": rows, "stats": stats})


@routes.post("/api/orders/create")
async def create(req):
    b, u, adm = await ctx(req)
    if not adm:
        hit(u["id"], "create", 6, 3600)
    async with user_lock(u["id"]):  # як клиент: ҳар дафъа як дархост (пахши дукарата ва ҳамзамонӣ)
        sv = next((x for x in await get_services() if x["key"] == b.get("service")), None)
        if not sv or not sv["active"]:
            raise web.HTTPBadRequest()
        st = await get_settings()
        if st.get("busy") == "1":
            raise web.HTTPForbidden(text="busy")
        row, open_ = await asyncio.gather(
            upsert_user(u), sb("GET", "orders", {"select": "id", "user_id": f"eq.{u['id']}", "status": f"in.{OPEN}"}))
        try:
            mx = int(st.get("max_open") or 3)
        except ValueError:
            mx = 3
        if len(open_) >= mx:
            raise web.HTTPTooManyRequests(text="limit")
        data = {str(k)[:40]: str(v)[:150] for k, v in list((b.get("data") or {}).items())[:10]}
        note = str(b.get("note", ""))[:800]
        img = b.get("image")
        if img and not ok_img(img, 3_000_000):
            raise web.HTTPBadRequest()
        since = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
        dup = await sb("GET", "orders", {"select": "id,data", "user_id": f"eq.{u['id']}",
                                         "service": f"eq.{sv['key']}", "created_at": f"gte.{since}"})
        if any(d["data"] == data for d in dup):
            return OK()  # ҳамон дархост аллакай сабт шуд
        disc = max(int(row.get("discount") or 0), int(row.get("loyalty_pct") or 0))
        code = str(b.get("promo") or "").strip().upper()[:30]
        if code:
            pr = await sb("GET", "promos", {"code": f"eq.{code}"})
            if (not pr or not pr[0]["active"] or pr[0]["uses_left"] <= 0
                    or (pr[0].get("expires_at") and ts(pr[0]["expires_at"]) < datetime.now(timezone.utc))
                    or await sb("GET", "promo_uses", {"code": f"eq.{code}", "user_id": f"eq.{u['id']}"})):
                raise web.HTTPBadRequest(text="promo")
            if not await use_promo(code, u["id"]):
                raise web.HTTPBadRequest(text="promo")
            disc = max(disc, int(pr[0]["pct"]))
        est = estimate(sv, data)  # бе тахфиф
        o = (await sb("POST", "orders", None, {"user_id": u["id"], "service": sv["key"], "data": data,
                                               "note": note, "has_image": bool(img), "est_price": est,
                                               "disc_pct": disc}))[0]
        if img:
            await sb("POST", "order_images", None, {"order_id": o["id"], "data": img})
    await tellk(ADMIN_ID, "new", id=o["id"], name=nm(sv["name"], await lang_of(ADMIN_ID)),
                user=u.get("username", "-"))
    return OK()


@routes.post("/api/orders/image")
async def order_image(req):
    b, u, adm = await ctx(req)
    o = await own_order(b, u["id"], adm)
    rows = await sb("GET", "order_images", {"order_id": f"eq.{o['id']}"})
    return web.json_response({"image": await img_of(rows[0]) if rows else None})


@routes.post("/api/orders/price")
async def price(req):
    b, u, adm = await ctx(req)
    if not adm:
        raise web.HTTPForbidden()
    o = await own_order(b, u["id"], True)
    base = int(b["price"]) - max(0, int(b.get("off") or 0))  # нарх минус тахфифи шумо (сомонӣ)
    if base <= 0:
        raise web.HTTPBadRequest()
    disc = int(o.get("disc_pct") or 0)  # тахфифи % (промокод ё реферал)
    pr = max(1, (base * (100 - disc) + 50) // 100)  # нархи ниҳоӣ барои клиент
    hours = min(1000.0, max(0.0, float(b.get("hours") or 0)))
    await sb("PATCH", "orders", {"id": f"eq.{o['id']}"},
             {"price": pr, "base_price": base, "status": "priced", "hours": hours or None})
    await audit(u["id"], "price", o["id"], f"{base} → {pr}")
    lg = await lang_of(o["user_id"])
    await tellk(o["user_id"], "price", id=o["id"], price=pr,
                hours=M[lg]["hours"].format(h=f"{hours:g}") if hours else "")
    return OK()


@routes.post("/api/orders/confirm")
async def confirm(req):
    b, u, adm = await ctx(req)
    o = await own_order(b, u["id"], False)
    if o["status"] != "priced":
        raise web.HTTPBadRequest()
    st = "unpaid" if b.get("accept") else "rejected"
    await sb("PATCH", "orders", {"id": f"eq.{o['id']}"}, {"status": st})
    await tellk(ADMIN_ID, "accepted" if b.get("accept") else "declined", id=o["id"])
    return OK()


@routes.post("/api/orders/pay")
async def pay(req):
    b, u, adm = await ctx(req)
    if not adm:
        hit(u["id"], "pay", 10, 3600)
    o = await own_order(b, u["id"], False)
    img = b.get("image")
    if o["status"] != "unpaid" or not ok_img(img, 3_000_000):
        raise web.HTTPBadRequest()
    await sb("POST", "order_files", None, {"order_id": o["id"], "kind": "receipt", "data": img})
    await sb("PATCH", "orders", {"id": f"eq.{o['id']}"}, {"status": "checking"})
    await tellk(ADMIN_ID, "receipt", id=o["id"])
    return OK()


@routes.post("/api/orders/paycheck")
async def paycheck(req):
    b, u, adm = await ctx(req)
    if not adm:
        raise web.HTTPForbidden()
    o = await own_order(b, u["id"], True)
    if o["status"] != "checking":
        raise web.HTTPBadRequest()
    if b.get("ok"):
        await sb("PATCH", "orders", {"id": f"eq.{o['id']}"},
                 {"status": "working", "started_at": datetime.now(timezone.utc).isoformat()})
        svc_ = next((x for x in await get_services() if x["key"] == o["service"]), None)
        await tellk(o["user_id"], "paid_ok_acc" if svc_ and svc_.get("acc") else "paid_ok", id=o["id"])
        await audit(u["id"], "pay_ok", o["id"])
    else:
        await sb("PATCH", "orders", {"id": f"eq.{o['id']}"}, {"status": "unpaid"})
        await tellk(o["user_id"], "paid_bad", id=o["id"])
        await audit(u["id"], "pay_bad", o["id"])
    return OK()


@routes.post("/api/orders/files")
async def order_files(req):
    b, u, adm = await ctx(req)
    o = await own_order(b, u["id"], adm)
    kind = "result" if b.get("kind") == "result" else "receipt"
    rows = await sb("GET", "order_files", {"order_id": f"eq.{o['id']}", "kind": f"eq.{kind}",
                                           "order": "id.desc", "limit": "5"})
    imgs = await asyncio.gather(*[img_of(r) for r in rows])
    return web.json_response({"files": [x for x in imgs if x]})


@routes.post("/api/orders/result")
async def result(req):
    b, u, adm = await ctx(req)
    if not adm:
        raise web.HTTPForbidden()
    o = await own_order(b, u["id"], True)
    if not ok_img(b.get("image"), 3_000_000):
        raise web.HTTPBadRequest()
    await sb("POST", "order_files", None, {"order_id": o["id"], "kind": "result", "data": b["image"]})
    return OK()


@routes.post("/api/orders/finish")
async def finish(req):
    b, u, adm = await ctx(req)
    if not adm:
        raise web.HTTPForbidden()
    o = await own_order(b, u["id"], True)
    await sb("PATCH", "orders", {"id": f"eq.{o['id']}"},
             {"status": "done", "done_at": datetime.now(timezone.utc).isoformat()})
    await tellk(o["user_id"], "done", id=o["id"])
    await audit(u["id"], "finish", o["id"])
    cl = (await sb("GET", "users", {"id": f"eq.{o['user_id']}"}))[0]
    if int(o.get("disc_pct") or 0) > 0 and cl.get("discount"):
        await sb("PATCH", "users", {"id": f"eq.{cl['id']}"}, {"discount": 0})
    if cl.get("ref_by"):
        n = await sb("GET", "orders", {"select": "id", "user_id": f"eq.{cl['id']}", "status": "eq.done"})
        ref = await sb("GET", "users", {"id": f"eq.{cl['ref_by']}"})
        if len(n) == 1 and ref:
            await sb("PATCH", "users", {"id": f"eq.{ref[0]['id']}"},
                     {"discount": min(30, int(ref[0].get("discount") or 0) + 10)})
            await tellk(ref[0]["id"], "ref_reward")
    return OK()


@routes.post("/api/orders/cancel")
async def cancel(req):
    b, u, adm = await ctx(req)
    o = await own_order(b, u["id"], adm)
    st = o["status"]
    if st in ("done", "cancelled", "rejected") or (not adm and st not in ("new", "priced", "unpaid", "checking")):
        raise web.HTTPBadRequest()
    reason = str(b.get("reason", "")).strip()[:200]
    await sb("PATCH", "orders", {"id": f"eq.{o['id']}"}, {"status": "cancelled", "cancel_reason": reason or None})
    await audit(u["id"], "cancel", o["id"], ("admin: " if adm else "client: ") + reason)
    if adm:
        await tellk(o["user_id"], "cancel_admin", id=o["id"], reason=reason or "—")
    else:
        await tellk(ADMIN_ID, "cancel_paid" if st == "checking" else "cancel_client", id=o["id"])
    return OK()


@routes.post("/api/orders/share")
async def share(req):
    b, u, adm = await ctx(req)
    if not adm:
        raise web.HTTPForbidden()
    o = await own_order(b, u["id"], True)
    ch = ((await get_settings()).get("channel") or "").strip()
    if not ch or not o.get("allow_share") or o.get("shared"):
        raise web.HTTPBadRequest()
    f = await sb("GET", "order_files", {"order_id": f"eq.{o['id']}", "kind": "eq.result",
                                        "order": "id.desc", "limit": "1"})
    if not f:
        raise web.HTTPBadRequest()
    svn = await sb("GET", "services", {"key": f"eq.{o['service']}"})
    rv = await sb("GET", "reviews", {"order_id": f"eq.{o['id']}"})
    cap = "✅ " + (svn[0]["name"] if svn else "")
    if rv:
        cap += f"\n⭐ {rv[0]['rating']}/5" + (f"\n«{rv[0]['text']}»" if rv[0]["text"] else "")
    try:
        img = base64.b64decode((await img_of(f[0])).split(",", 1)[1])
        await bot.send_photo(ch, BufferedInputFile(img, "r.jpg"), caption=cap[:1000])
    except Exception:
        raise web.HTTPBadGateway(text="channel")
    await sb("PATCH", "orders", {"id": f"eq.{o['id']}"}, {"shared": True})
    await audit(u["id"], "share", o["id"])
    return OK()


@routes.post("/api/chat/list")
async def chat_list(req):
    b, u, adm = await ctx(req)
    o = await own_order(b, u["id"], adm)
    rows = await sb("GET", "messages", {"select": "id,sender_id,text,has_img,created_at", "order_id": f"eq.{o['id']}",
                                        "order": "id.asc", "limit": "200"})
    if rows:
        col = "admin_read_id" if adm else "client_read_id"
        if rows[-1]["id"] > (o.get(col) or 0):
            await sb("PATCH", "orders", {"id": f"eq.{o['id']}"}, {col: rows[-1]["id"]})
    return web.json_response({"messages": [
        {"id": m["id"], "mine": m["sender_id"] == u["id"], "sys": m["sender_id"] == 0,
         "text": m["text"], "img": bool(m.get("has_img")), "t": m["created_at"]} for m in rows]})


@routes.post("/api/chat/image")
async def chat_image(req):
    b, u, adm = await ctx(req)
    o = await own_order(b, u["id"], adm)
    r = await sb("GET", "messages", {"select": "image,file_id", "id": f"eq.{int(b['mid'])}", "order_id": f"eq.{o['id']}"})
    return web.json_response({"image": await img_of(r[0], "image") if r else None})


@routes.post("/api/chat/send")
async def chat_send(req):
    b, u, adm = await ctx(req)
    if not adm:
        hit(u["id"], "chat", 20, 60)
    o = await own_order(b, u["id"], adm)
    text = str(b.get("text", "")).strip()[:1000]
    img = b.get("image")
    if img and not ok_img(img, 1_500_000):
        raise web.HTTPBadRequest()
    if not text and not img:
        raise web.HTTPBadRequest()
    await sb("POST", "messages", None, {"order_id": o["id"], "sender_id": u["id"], "text": text,
                                        "image": img, "has_img": bool(img)})
    await tellk(o["user_id"] if adm else ADMIN_ID, "chat", id=o["id"], text=text[:200] or "📷")
    if not adm:
        st = await get_settings()
        if st.get("hours") and not in_hours(st["hours"]):
            last = await sb("GET", "messages", {"select": "created_at", "order_id": f"eq.{o['id']}",
                                                "sender_id": "eq.0", "order": "id.desc", "limit": "1"})
            if not last or datetime.now(timezone.utc) - ts(last[0]["created_at"]) > timedelta(hours=6):
                await sb("POST", "messages", None, {"order_id": o["id"], "sender_id": 0,
                                                    "text": st.get("away") or AWAY})
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
    return web.json_response({"items": await get_services()})


@routes.post("/api/services/toggle")
async def services_toggle(req):
    b, u, adm = await ctx(req)
    if not adm:
        raise web.HTTPForbidden()
    await sb("PATCH", "services", {"key": f"eq.{b.get('key')}"}, {"active": bool(b.get("active"))})
    _c.clear()
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
    unit = b.get("unit_price")
    unit = max(0, int(unit)) if unit not in (None, "") else None
    fields = []
    for f in (b.get("fields") or [])[:8]:
        label = str(f.get("label", "")).strip()[:50] if isinstance(f, dict) else ""
        if label:
            role = f.get("role") if f.get("role") in ("from", "to", "count") else None
            item = {"label": label, "type": "number" if (role or f.get("type") == "number") else "text",
                    "opt": bool(f.get("opt"))}
            if role:
                item["role"] = role
            fields.append(item)
    row = {"key": key or "s" + str(int(time.time())), "name": name,
           "icon": str(b.get("icon") or "").strip()[:8] or "🛠️", "price": pr, "fields": fields,
           "unit_price": unit, "acc": bool(b.get("acc")), "active": bool(b.get("active"))}
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
    _c.clear()
    return OK()


@routes.post("/api/settings")
async def settings(req):
    b, u, adm = await ctx(req)
    s = await get_settings()
    q = cache_get("queue", 10)
    if q is None:
        q = cache_set("queue", len(await sb("GET", "orders", {"select": "id", "status": "in.(unpaid,checking,working)", "limit": "1000"})))
    keys = SETTING_KEYS if adm else ("pay_info", "rules", "busy", "hours")
    out = {k: s.get(k, "") for k in keys}
    out["queue"] = q
    return web.json_response(out)


@routes.post("/api/settings/save")
async def settings_save(req):
    b, u, adm = await ctx(req)
    if not adm:
        raise web.HTTPForbidden()
    vals = b.get("values") or {}
    rows = [{"key": k, "value": str(vals[k]).strip()[:3000]} for k in SETTING_KEYS if k in vals]
    reset = "storage_chat" in vals and str(vals["storage_chat"]).strip() != (await get_settings()).get("storage_chat", "")
    if rows:
        await sb("POST", "settings", {"on_conflict": "key"}, rows)
        if reset:
            await sb("POST", "settings", {"on_conflict": "key"},
                     [{"key": "tp_" + k, "value": ""} for k in STORE_KINDS])
        _c.clear()
    await audit(u["id"], "settings", None, ",".join(k for k in SETTING_KEYS if k in vals))
    return OK()


@routes.post("/api/promos")
async def promos(req):
    b, u, adm = await ctx(req)
    if not adm:
        raise web.HTTPForbidden()
    return web.json_response({"items": await sb("GET", "promos", {"order": "code.asc"})})


@routes.post("/api/promo/save")
async def promo_save(req):
    b, u, adm = await ctx(req)
    if not adm:
        raise web.HTTPForbidden()
    code = str(b.get("code", "")).strip().upper()
    if not re.fullmatch(r"[A-Z0-9_-]{2,30}", code):
        raise web.HTTPBadRequest()
    pct = min(90, max(1, int(b.get("pct", 10))))
    uses = min(10000, max(1, int(b.get("uses", 1))))
    days = b.get("days")
    exp = (datetime.now(timezone.utc) + timedelta(days=min(365, max(1, int(days))))).isoformat() if days not in (None, "", 0) else None
    await sb("POST", "promos", {"on_conflict": "code"},
             {"code": code, "pct": pct, "uses_left": uses, "active": True, "expires_at": exp})
    return OK()


@routes.post("/api/promo/del")
async def promo_del(req):
    b, u, adm = await ctx(req)
    if not adm:
        raise web.HTTPForbidden()
    await sb("DELETE", "promos", {"code": f"eq.{str(b.get('code', ''))[:30]}"})
    return OK()


@routes.post("/api/review/create")
async def review_create(req):
    b, u, adm = await ctx(req)
    if not adm:
        hit(u["id"], "rev", 10, 3600)
    o = await own_order(b, u["id"], False)
    if o["status"] != "done":
        raise web.HTTPBadRequest()
    rating = min(5, max(1, int(b.get("rating", 5))))
    text = str(b.get("text", "")).strip()[:500]
    await sb("POST", "reviews", None, {"order_id": o["id"], "user_id": u["id"], "rating": rating, "text": text})
    if b.get("allow_share"):
        await sb("PATCH", "orders", {"id": f"eq.{o['id']}"}, {"allow_share": True})
    if o.get("archived"):
        asyncio.create_task(late_review(o, rating, text))
    else:
        asyncio.create_task(archive_order(o["id"]))
    await tellk(ADMIN_ID, "review", id=o["id"], rating=rating, text=text)
    return OK()


@routes.post("/api/profile")
async def profile(req):
    b, u, adm = await ctx(req)
    mine = await upsert_user(u)
    master = mine if adm else ((await sb("GET", "users", {"id": f"eq.{ADMIN_ID}"})) or [{}])[0]
    rv = await sb("GET", "reviews", {"select": "rating,text,users(name)", "order": "id.desc", "limit": "5000"})
    done = await sb("GET", "orders", {"select": "id", "status": "eq.done", "limit": "5000"})
    avg = round(sum(r["rating"] for r in rv) / len(rv), 1) if rv else 0
    pick = lambda r: {"name": r.get("name") or "Мутахассис", "avatar": r.get("avatar"), "about": r.get("about") or "",
                      "tag": r.get("tag") or "", "clan_tag": r.get("clan_tag") or ""}
    return web.json_response({
        "me": {**pick(mine), "discount": mine.get("discount") or 0, "loyalty": mine.get("loyalty_pct") or 0, "id": u["id"]},
        "bot": BOT_NAME,
        "master": {**pick(master), "avg": avg, "count": len(rv), "done": len(done),
                   "reviews": [{"rating": r["rating"], "text": r["text"], "name": (r.get("users") or {}).get("name", "")}
                               for r in rv[:20]]}})


@routes.post("/api/profile/update")
async def profile_update(req):
    b, u, adm = await ctx(req)
    if not adm:
        hit(u["id"], "prof", 10, 60)
    await upsert_user(u)
    patch = {"name": str(b.get("name", "")).strip()[:40] or u.get("first_name", ""),
             "about": str(b.get("about", "")).strip()[:300],
             "tag": str(b.get("tag", "")).strip()[:30], "clan_tag": str(b.get("clan_tag", "")).strip()[:30]}
    av = b.get("avatar")
    if av:
        if not ok_img(av, 100_000):
            raise web.HTTPBadRequest()
        patch["avatar"] = av
    await sb("PATCH", "users", {"id": f"eq.{u['id']}"}, patch)
    return OK()


@routes.post("/api/finance")
async def finance(req):
    b, u, adm = await ctx(req)
    if not adm:
        raise web.HTTPForbidden()
    rows = await sb("GET", "orders", {"select": "status,price,created_at,done_at,service,user_id,users(name,username)",
                                      "price": "not.is.null", "limit": "5000"})
    now = datetime.now(timezone.utc)
    week = month = total = wait = cnt = 0
    days = [0] * 7
    bys, top = {}, {}
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
            s = bys.setdefault(r["service"], [0, 0])
            s[0] += 1
            s[1] += pr
            usr = r.get("users") or {}
            c = top.setdefault(r["user_id"], [0, 0, usr.get("username") or usr.get("name") or str(r["user_id"])])
            c[0] += 1
            c[1] += pr
        elif r["status"] == "working":
            wait += pr
    return web.json_response({
        "week": week, "month": month, "total": total, "wait": wait, "count": cnt, "days": days,
        "svc": [{"key": k, "count": v[0], "sum": v[1]} for k, v in sorted(bys.items(), key=lambda x: -x[1][1])],
        "top": [{"name": v[2], "count": v[0], "sum": v[1]} for v in sorted(top.values(), key=lambda x: -x[1])[:5]]})


async def send_export(chat_id):
    orders_ = await sb("GET", "orders", {"select": "id,user_id,service,status,price,hours,created_at,done_at,note,data,cancel_reason",
                                         "order": "id.asc", "limit": "20000"})
    users_ = await sb("GET", "users", {"select": "id,name,username,lang,created_at", "limit": "20000"})
    for fname, rows in (("orders.csv", orders_), ("users.csv", users_)):
        if not rows:
            continue
        buf = io.StringIO()
        w = csv.writer(buf)
        cols = list(rows[0].keys())
        w.writerow(cols)
        for r in rows:
            w.writerow([json.dumps(r[c], ensure_ascii=False) if isinstance(r[c], (dict, list)) else r[c] for c in cols])
        await bot.send_document(chat_id, BufferedInputFile(buf.getvalue().encode("utf-8-sig"), fname))


@routes.post("/api/storage/test")
async def storage_test(req):
    b, u, adm = await ctx(req)
    if not adm:
        raise web.HTTPForbidden()
    cid = await store_chat()
    if not cid:
        raise web.HTTPBadRequest(text="no id")
    try:
        await bot.send_message(cid, "✅ Нигоҳдорӣ кор мекунад / Хранилище работает")
        topics = [await topic_for(k) for k in STORE_KINDS]
    except Exception as e:
        raise web.HTTPBadGateway(text=str(e)[:200])
    return web.json_response({"ok": True, "forum": any(topics)})


@routes.post("/api/migrate")
async def migrate(req):
    """Ҳамаи фармоишҳои анҷомшударо ҳозир ба архив мегузорад (дар як маротиба 3 то, ки Telegram зиёд иҷозат намедиҳад)."""
    b, u, adm = await ctx(req)
    if not adm:
        raise web.HTTPForbidden()
    if not await store_chat():
        raise web.HTTPBadRequest(text="no storage")
    moved = 0
    for o in await sb("GET", "orders", {"select": "id", "status": "eq.done", "archived": "eq.false", "limit": "3"}):
        if await archive_order(o["id"]):
            moved += 1
        await asyncio.sleep(2)
    left = len(await sb("GET", "orders", {"select": "id", "status": "eq.done", "archived": "eq.false", "limit": "1000"}))
    return web.json_response({"moved": moved, "left": left})


@routes.post("/api/user/ban")
async def user_ban(req):
    b, u, adm = await ctx(req)
    if not adm:
        raise web.HTTPForbidden()
    uid = int(b["user_id"])
    if uid == ADMIN_ID:
        raise web.HTTPBadRequest()
    on = bool(b.get("banned"))
    await sb("PATCH", "users", {"id": f"eq.{uid}"}, {"banned": on})
    await audit(u["id"], "ban" if on else "unban", None, str(uid))
    (BANNED.add if on else BANNED.discard)(uid)
    return OK()


@routes.post("/api/user/loyalty")
async def user_loyalty(req):
    b, u, adm = await ctx(req)
    if not adm:
        raise web.HTTPForbidden()
    uid = int(b["user_id"])
    pct = min(50, max(0, int(b.get("pct", 0))))
    await sb("PATCH", "users", {"id": f"eq.{uid}"}, {"loyalty_pct": pct})
    await audit(u["id"], "loyalty", None, f"{uid}: {pct}%")
    if pct:
        await tellk(uid, "loyalty", pct=pct)
    return OK()


@routes.post("/api/audit")
async def audit_list(req):
    b, u, adm = await ctx(req)
    if not adm:
        raise web.HTTPForbidden()
    return web.json_response({"items": await sb("GET", "audit_log", {"order": "id.desc", "limit": "100"})})


@routes.post("/api/export")
async def export(req):
    b, u, adm = await ctx(req)
    if not adm:
        raise web.HTTPForbidden()
    await send_export(ADMIN_ID)
    return OK()


async def background():
    """Ёдраскунӣ, бекоркунии худкор, нусхаи ҳафтаина. Ҳар 30 дақиқа ба база муроҷиат мекунад (хомӯш намешавад)."""
    while True:
        try:
            now = datetime.now(timezone.utc)
            rows = await sb("GET", "orders", {"select": "id,user_id,status,status_at,reminded",
                                              "status": "in.(priced,unpaid)", "limit": "500"})
            for o in rows:
                age = now - ts(o["status_at"])
                if age > timedelta(hours=48):
                    await sb("PATCH", "orders", {"id": f"eq.{o['id']}"},
                             {"status": "cancelled", "cancel_reason": "Auto: no reply / ҷавоб нарасид"})
                    await tellk(o["user_id"], "auto_cancel", id=o["id"])
                elif age > timedelta(hours=24) and not o["reminded"]:
                    await sb("PATCH", "orders", {"id": f"eq.{o['id']}"}, {"reminded": True})
                    await tellk(o["user_id"], "remind_priced" if o["status"] == "priced" else "remind_unpaid", id=o["id"])
            if await store_chat():
                cut3 = (now - timedelta(days=3)).isoformat()
                for o in await sb("GET", "orders", {"select": "id", "status": "eq.done", "archived": "eq.false",
                                                    "done_at": f"lt.{cut3}", "limit": "3"}):
                    await archive_order(o["id"])
                    await asyncio.sleep(2)
            cut30 = (now - timedelta(days=30)).isoformat()
            for o in await sb("GET", "orders", {"select": "id", "status": "in.(cancelled,rejected)",
                                                "purged": "eq.false", "status_at": f"lt.{cut30}", "limit": "20"}):
                await purge_media(o["id"])
            for k in [k for k, v in _hits.items() if not v or time.time() - v[-1] > 3600]:
                _hits.pop(k, None)
            s = await sb("GET", "settings", {"key": "eq.last_backup"})
            last = ts(s[0]["value"]) if s and s[0]["value"] else None
            if not last or now - last > timedelta(days=7):
                await send_export(ADMIN_ID)
                await sb("POST", "settings", {"on_conflict": "key"}, [{"key": "last_backup", "value": now.isoformat()}])
        except Exception as e:
            print("background:", e)
        await asyncio.sleep(1800)


@web.middleware
async def cors(req, handler):
    try:
        r = web.Response() if req.method == "OPTIONS" else await handler(req)
    except web.HTTPException as e:
        r = e
    r.headers.update({"Access-Control-Allow-Origin": "*",
                      "Access-Control-Allow-Headers": "*",
                      "Access-Control-Allow-Methods": "GET, POST, OPTIONS"})
    return r


@dp.message(CommandStart())
async def start(m: Message):
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) > 1 and parts[1].startswith("ref_") and parts[1][4:].isdigit():
        ref, uid = int(parts[1][4:]), m.from_user.id
        if ref != uid:
            try:
                if not await sb("GET", "users", {"id": f"eq.{uid}"}) and await sb("GET", "users", {"id": f"eq.{ref}"}):
                    await sb("POST", "users", None, {"id": uid, "name": m.from_user.first_name or "",
                                                     "username": m.from_user.username or "",
                                                     "ref_by": ref, "discount": 10})
            except Exception:
                pass
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Кушодани барнома / Открыть", web_app=WebAppInfo(url=WEBAPP_URL))]])
    await m.answer("Салом! Барои фармоиш тугмаро пахш кунед.\nПривет! Нажмите кнопку, чтобы сделать заказ.",
                   reply_markup=kb)


@dp.message(Command("id"))
async def cmd_id(m: Message):
    await m.reply(f"ID: {m.chat.id}")


@dp.channel_post(Command("id"))
async def cmd_id_channel(m: Message):
    await bot.send_message(m.chat.id, f"ID: {m.chat.id}")


async def main():
    global BOT_NAME, SESSION
    SESSION = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30),
                                    connector=aiohttp.TCPConnector(limit=60, ttl_dns_cache=300))
    try:
        for r in await sb("GET", "users", {"select": "id", "banned": "eq.true", "limit": "5000"}):
            BANNED.add(r["id"])
    except Exception as e:
        print("banned load:", e)
    app = web.Application(middlewares=[cors], client_max_size=8 * 1024 * 1024)
    app.add_routes(routes)
    app.router.add_route("OPTIONS", "/{tail:.*}", lambda r: web.Response())
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    BOT_NAME = (await bot.get_me()).username or ""
    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
            text="Фармоиш / Заказ", web_app=WebAppInfo(url=WEBAPP_URL)))
    except Exception:
        pass
    bg = asyncio.create_task(background())
    try:
        await dp.start_polling(bot)
    finally:
        bg.cancel()
        await SESSION.close()


if __name__ == "__main__":
    asyncio.run(main())
