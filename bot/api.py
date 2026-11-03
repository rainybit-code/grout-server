"""Small HTTP API next to the bot, behind your HTTPS reverse proxy.

Every request needs:  Authorization: Bearer <API_TOKEN>

  GET  /health                 no auth, for uptime checks
  GET  /stats                  streaks, this week, last week
  GET  /dashboard?days=35      everything the app shows: streaks, check-ins, weeks,
                               targets, daily history, notification schedule
  GET  /notify/<kind>          checkin | nudge | training | review: what to say now,
                               {"notification": null} when there is nothing to say
  POST /checkin   {"day"?, "move_lvl"?, "read_lvl"?, "phone_out"?, "goals"?,
                   "screen": "4h10" | "screen_min": 250, "nav"?: "45m"}
                  (nav defaults to the usual drive on DRIVE_DAYS, else 0)
  POST /book      {"title", "total_pages", "current_page"?}   start a book; current_page = already
                                                 read before (progress only, not reading done)
  POST /book/done                                finish the current book
  POST /book/page {"page", "day"?}               "I'm on page N": logs the pages since last time
  POST /sync      {"since": rev, "changes": {table: [rows]}}   two-way sync with the phone app
  POST /workout   {"kind", "minutes" | "duration": "1h05", "km"?, "day"?}
  POST /reading   {"pages"?, "minutes"?, "day"?}

"day" is YYYY-MM-DD and defaults to today (TZ, 04:00 rollover).
"""

import datetime as dt
import decimal
import hmac
import json
import logging

from aiohttp import web

import notify
import sync
from core import Store, parse_duration, today

STORE = web.AppKey("store", Store)
log = logging.getLogger("habits.api")


def _json_default(o):
    if isinstance(o, (dt.date, dt.datetime)):
        return o.isoformat()
    if isinstance(o, decimal.Decimal):
        return float(o)
    raise TypeError(type(o))


def ok(data, status=200):
    return web.json_response(data, status=status, dumps=lambda d: json.dumps(d, default=_json_default))


def bad(msg, status=400):
    return ok({"error": msg}, status)


async def body(request) -> dict:
    try:
        data = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text='{"error": "body must be JSON"}', content_type="application/json")
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text='{"error": "body must be a JSON object"}', content_type="application/json")
    return data


def get_day(data) -> dt.date:
    if not data.get("day"):
        return today()
    try:
        return dt.date.fromisoformat(str(data["day"]))
    except ValueError:
        raise web.HTTPBadRequest(text='{"error": "day must be YYYY-MM-DD"}', content_type="application/json")


def auth_middleware(token: str):
    @web.middleware
    async def mw(request, handler):
        if request.path != "/health":
            given = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            if not token or not hmac.compare_digest(given, token):
                return bad("unauthorized", 401)
        return await handler(request)
    return mw


async def health(request):
    await request.app[STORE].fetch("SELECT 1")
    return ok({"ok": True})


async def stats(request):
    s = request.app[STORE]
    return ok({"today": today(), "streaks": await s.streaks(),
               "this_week": await s.week(0), "last_week": await s.week(1)})


HISTORY_COLS = ("day", "tracking", "move_lvl", "read_lvl", "phone_out", "screen_min", "nav_min",
                "screen_net_min", "workouts", "pages", "move_hit", "read_hit", "phone_hit", "all_hit")


async def dashboard(request):
    s, d = request.app[STORE], today()
    try:
        days = max(1, min(int(request.query.get("days", 35)), 366))
    except ValueError:
        return bad("days must be a number")
    rows = await s.fetch(f"SELECT {', '.join(HISTORY_COLS)} FROM v_daily "
                         "WHERE day > %s::date - %s AND day <= %s ORDER BY day", (d, days, d))
    goals = dict(await s.fetch("SELECT day, goals FROM checkins WHERE day > %s::date - %s AND goals IS NOT NULL",
                               (d, days)))
    history = [dict(zip(HISTORY_COLS, r)) for r in rows]
    for h in history:
        h["goals"] = goals.get(h["day"])
    return ok({
        "today": d,
        "checkin": {"today": await s.checkin_state(d),
                    "yesterday": await s.checkin_state(d - dt.timedelta(days=1))},
        "streaks": {**await s.streaks(), "goals": await s.goals_streak()},
        "this_week": await s.week(0),
        "last_week": await s.week(1),
        "targets": {k: v for k, v in await s.fetch("SELECT key, value FROM targets")},
        "history": history,
        "schedule": notify.SCHEDULE,
        "book": await s.current_book(),
        "books_finished": [dict(zip(("title", "started", "finished", "total_pages"), r)) for r in await s.fetch(
            "SELECT title, started, finished, total_pages FROM books WHERE finished IS NOT NULL AND NOT deleted "
            "ORDER BY finished DESC, id DESC LIMIT 20")],
        "books_finished_this_year": (await s.fetchone(
            "SELECT count(*) FROM books WHERE finished >= date_trunc('year', current_date) AND NOT deleted"))[0],
    })


async def notification(request):
    fn = notify.KINDS.get(request.match_info["kind"])
    if not fn:
        return bad("unknown kind", 404)
    n = await fn(request.app[STORE])
    log.info("notify %s (%s): %s", request.match_info["kind"], request.query.get("from", "-"),
             n["title"] if n else "nothing due")
    return ok({"notification": n})


async def sync_endpoint(request):
    data = await body(request)
    try:
        since = int(data.get("since") or 0)
        changes = data.get("changes") or {}
        if not isinstance(changes, dict):
            raise ValueError("changes must be an object")
        return ok(await sync.apply(request.app[STORE], since, changes))
    except (KeyError, ValueError, TypeError) as e:
        return bad(f"bad sync payload: {e}")


async def book_start(request):
    data = await body(request)
    title = str(data.get("title", "")).strip()
    pages = data.get("total_pages")
    current = data.get("current_page", 0) or 0
    if not title or not isinstance(pages, int) or pages <= 0:
        return bad("need title and total_pages > 0")
    if not isinstance(current, int) or not 0 <= current < pages:
        return bad("current_page must be between 0 and total_pages")
    return ok({"book": await request.app[STORE].start_book(title, pages, current)}, 201)


async def book_done(request):
    r = await request.app[STORE].finish_book()
    return ok(r) if r else bad("no book in progress", 404)


async def book_page(request):
    data = await body(request)
    s, page = request.app[STORE], data.get("page")
    book = await s.current_book()
    if not book:
        return bad("no book in progress", 404)
    if not isinstance(page, int) or page < 0:
        return bad("page must be a whole number")
    diff = page - book["pages_read"]
    if diff <= 0:
        return bad(f"already at page {book['pages_read']}")
    out = await s.log_reading(get_day(data), pages=diff)
    out["book"] = await s.current_book()
    out["logged_pages"] = diff
    return ok(out, 201)


async def checkin(request):
    data = await body(request)
    day = get_day(data)
    fields = {}
    for col in ("move_lvl", "read_lvl"):
        if col in data:
            if data[col] not in (0, 1, 2):
                return bad(f"{col} must be 0, 1 or 2")
            fields[col] = data[col]
    for col in ("phone_out", "goals"):
        if col in data:
            if not isinstance(data[col], bool):
                return bad(f"{col} must be true or false")
            fields[col] = data[col]
    screen = nav = None
    if "screen" in data or "screen_min" in data:
        screen = parse_duration(data.get("screen", data.get("screen_min")))
        if screen is None:
            return bad("screen must look like 4h10 or 250")
        if "nav" in data or "nav_min" in data:
            nav = parse_duration(data.get("nav", data.get("nav_min")))
            if nav is None:
                return bad("nav must look like 45m or 45")
    if not fields and screen is None:
        return bad("nothing to log")
    s = request.app[STORE]
    await s.set_checkin(day, **fields)
    if screen is not None:
        await s.log_screen(day, screen, nav)
    return ok({"day": day, "checkin": await s.checkin_state(day), "milestones": await s.milestones(day)})


async def workout(request):
    data = await body(request)
    kind = str(data.get("kind", "")).strip()
    minutes = parse_duration(data.get("duration", data.get("minutes", "")))
    if not kind or not minutes:
        return bad("kind and minutes (or duration) are required")
    km = data.get("km")
    if km is not None and not isinstance(km, (int, float)):
        return bad("km must be a number")
    return ok(await request.app[STORE].log_workout(get_day(data), kind, minutes, km), 201)


async def reading(request):
    data = await body(request)
    pages, minutes = data.get("pages"), data.get("minutes")
    if pages is None and minutes is None:
        return bad("pages or minutes is required")
    for v in (pages, minutes):
        if v is not None and (not isinstance(v, int) or v < 0):
            return bad("pages and minutes must be whole numbers")
    return ok(await request.app[STORE].log_reading(get_day(data), pages, minutes), 201)


def build_app(store: Store, token: str) -> web.Application:
    app = web.Application(middlewares=[auth_middleware(token)])
    app[STORE] = store
    app.add_routes([web.get("/health", health), web.get("/stats", stats),
                    web.get("/dashboard", dashboard), web.get("/notify/{kind}", notification),
                    web.post("/checkin", checkin), web.post("/workout", workout),
                    web.post("/reading", reading), web.post("/book", book_start),
                    web.post("/book/done", book_done), web.post("/book/page", book_page),
                    web.post("/sync", sync_endpoint)])
    return app


async def start(store: Store, token: str, port: int) -> web.AppRunner:
    runner = web.AppRunner(build_app(store, token), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    return runner
