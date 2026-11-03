"""Habit tracker: Telegram bot + small HTTP API, logging to Postgres. Stays quiet.

Messages it sends on its own (everything else is a reply to you):
  07:00          write today's 3 goals, only if not ticked off yet
  11:00 / 19:20  move / read reminder, only if not logged yet today
  21:30          evening check-in (skipped if the day is already logged)
  22:30          one line, only if a streak of 3+ days is about to break
  Sat 10:00      one line, only if you're behind on weekly training
  Sun 19:00      weekly summary, delivered silently (no sound)
Milestones (7, 14, 30... days, or a new best) are added to your own replies,
never pushed separately.
"""

import datetime as dt
import logging
import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, filters)

import api
import notify
from core import NAMES, TZ, Store, day_label, fmt_min, parse_duration, plural, today

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("habits")

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")  # empty = HTTP API only, no bot
CHAT_ID = int(os.environ.get("TELEGRAM_CHAT_ID") or 0)  # 0 = not set yet, use /whoami
DSN = os.environ["DATABASE_URL"]
API_TOKEN = os.environ.get("API_TOKEN", "")
API_PORT = int(os.environ.get("API_PORT", "8080"))


# Scheduled messages also go to Telegram unless NOTIFY_TELEGRAM=false (e.g. when the
# Android app shows them as notifications instead).
NOTIFY_TELEGRAM = os.environ.get("NOTIFY_TELEGRAM", "true").strip().lower() not in ("0", "false", "no", "off")


def hhmm(kind):
    h, m = notify.SCHEDULE[kind]["time"].split(":")
    return dt.time(int(h), int(m), tzinfo=TZ)


CHECKIN_AT = hhmm("checkin")
NUDGE_AT = hhmm("nudge")
TRAINING_CHECK_AT = hhmm("training")
REVIEW_AT = hhmm("review")

BUTTONS = {  # column -> (label, options)
    "goals":     ("3 goals",   [(True, "written"), (False, "no")]),
    "move_lvl":  ("Move",      [(0, "none"), (1, "2 min"), (2, "session")]),
    "read_lvl":  ("Read",      [(0, "none"), (1, "10 pages"), (2, "30 min")]),
    "phone_out": ("Phone out", [(True, "yes"), (False, "no")]),
}


def store(ctx: ContextTypes.DEFAULT_TYPE) -> Store:
    return ctx.application.bot_data["store"]


def pick_day(args: list) -> dt.date:
    """A trailing 'y' or 'yesterday' means yesterday."""
    if args and args[-1].lower() in ("y", "yesterday"):
        args.pop()
        return today() - dt.timedelta(days=1)
    return today()


def mine(update: Update) -> bool:
    return bool(CHAT_ID) and update.effective_chat is not None and update.effective_chat.id == CHAT_ID


# ---------------------------------------------------------------- check-in

async def render_checkin(s: Store, day: dt.date):
    state = await s.checkin_state(day)
    lines, kb = [day.strftime("%a %d %b")], []
    for col, (label, options) in BUTTONS.items():
        current = state[col]
        lines.append(f"{label}: " + next((t for v, t in options if v == current), "-"))
        kb.append([InlineKeyboardButton(
            ("• " if v == current else "") + (f"{label}: {t}" if i == 0 else t),
            callback_data=f"c|{day.isoformat()}|{col}|{int(v)}")
            for i, (v, t) in enumerate(options)])
    if all(state[c] is not None for c in BUTTONS):
        a = (await s.streaks())["all"]
        lines += ["", f"Logged. All-three streak: {a['current']} (best {a['best']})."]
        lines += await s.milestones(day)
    return "\n".join(lines), InlineKeyboardMarkup(kb)


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not mine(update):
        return
    _, day_s, col, raw = q.data.split("|")
    if col not in BUTTONS:
        return
    day = dt.date.fromisoformat(day_s)
    await store(ctx).set_checkin(day, **{col: bool(int(raw)) if col in ("phone_out", "goals") else int(raw)})
    text, kb = await render_checkin(store(ctx), day)
    if text != q.message.text:
        await q.edit_message_text(text, reply_markup=kb)


async def cmd_checkin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text, kb = await render_checkin(store(ctx), pick_day(list(ctx.args)))
    await update.message.reply_text(text, reply_markup=kb)


# ---------------------------------------------------------------- logging commands

SCREEN_USAGE = ("Usage: /screen 4h10  (phone's total)\n"
                "Navigation is subtracted on drive days. Override: /screen 4h10 drive 45, "
                "or /screen 4h10 nodrive. Add y for yesterday.")


async def cmd_screen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/screen 4h10 [drive 45 | nodrive] [y]"""
    args = [a.lower() for a in ctx.args]
    day = pick_day(args)
    nav = None
    if "nodrive" in args:
        args.remove("nodrive")
        nav = 0
    for kw in ("drive", "nav"):
        if kw in args:
            i = args.index(kw)
            nav = parse_duration(args[i + 1]) if i + 1 < len(args) else None
            if nav is None:
                await update.message.reply_text(SCREEN_USAGE)
                return
            del args[i:i + 2]
    minutes = parse_duration(" ".join(args)) if args else None
    if minutes is None:
        await update.message.reply_text(SCREEN_USAGE)
        return
    r = await store(ctx).log_screen(day, minutes, nav)
    nav_part = f" minus {fmt_min(r['nav_min'])} driving = {fmt_min(r['net_min'])}" if r["nav_min"] else ""
    await update.message.reply_text(
        f"{day_label(day)}: {fmt_min(minutes)}{nav_part}. "
        f"7-day avg {fmt_min(r['avg7_net_min'])}, target {fmt_min(r['target_min'])}.")


async def cmd_workout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = list(ctx.args)
    day = pick_day(args)
    minutes = parse_duration(args[1]) if len(args) >= 2 else None
    try:
        km = float(args[2].replace(",", ".")) if len(args) > 2 else None
    except ValueError:
        km = minutes = None
    if not minutes:
        await update.message.reply_text("Usage: /workout ride 45 18.5  (kind, minutes, km; add y for yesterday)")
        return
    r = await store(ctx).log_workout(day, args[0], minutes, km)
    msg = f"{day_label(r['day'])}: {r['kind']}, {fmt_min(minutes)}" + (f", {km:g} km" if km else "")
    msg += f". {r['week_workouts']} of {r['week_target']} this week."
    await update.message.reply_text("\n".join([msg] + r["milestones"]))


async def cmd_read(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/read 25 (pages) | /read 30m (minutes) | /read 20 25m"""
    args = list(ctx.args)
    day = pick_day(args)
    pages = minutes = None
    for a in (x.lower() for x in args):
        if a.endswith(("m", "min")):
            minutes = parse_duration(a.removesuffix("in"))
        elif a.rstrip("p").isdigit():
            pages = int(a.rstrip("p"))
    if pages is None and minutes is None:
        await update.message.reply_text("Usage: /read 25  (pages) or /read 30m  (minutes)")
        return
    r = await store(ctx).log_reading(day, pages, minutes)
    msg = f"{day_label(day)}: {r['day_pages']} pages" + (f", {r['day_minutes']} min" if r["day_minutes"] else "") + "."
    b = r["book"]
    if b and b["total_pages"]:
        msg += f" {b['title']}: {b['pages_read']}/{b['total_pages']} ({b['pct']:.0f}%)."
    await update.message.reply_text("\n".join([msg] + r["milestones"]))


async def cmd_book(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/book 320 Title of book [p75]  |  /book done   (p75 = already on page 75)"""
    s, args = store(ctx), list(ctx.args)
    if args and args[0].lower() == "done":
        r = await s.finish_book()
        await update.message.reply_text(f"Finished {r['title']}. Book {r['finished_this_year']} this year."
                                        if r else "No book in progress.")
        return
    if len(args) < 2 or not args[0].isdigit():
        cur = await s.fetchone("""SELECT title, pages_read, total_pages FROM v_books
                                  WHERE finished IS NULL ORDER BY started DESC, id DESC LIMIT 1""")
        hint = f"Reading: {cur[0]} ({cur[1]}/{cur[2]}).\n" if cur else ""
        await update.message.reply_text(hint + "Usage: /book 320 Title  (start; add p75 if already on page 75)  or  /book done")
        return
    start = 0
    if len(args) > 2 and args[-1].lower().startswith("p") and args[-1][1:].isdigit():
        start = int(args.pop()[1:])
    title = " ".join(args[1:])
    await s.start_book(title, int(args[0]), min(start, int(args[0]) - 1))
    await update.message.reply_text(f"Started {title}, {args[0]} pages" + (f", from page {start}." if start else "."))


async def cmd_undo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Remove the most recent workout or reading entry."""
    s = store(ctx)
    row = await s.fetchone("""SELECT 'workouts', id, created_at FROM workouts WHERE NOT deleted
                              UNION ALL SELECT 'reading', id, created_at FROM reading WHERE NOT deleted
                              ORDER BY created_at DESC LIMIT 1""")
    if not row:
        await update.message.reply_text("Nothing to undo.")
        return
    # Soft delete, so the phone app learns about it on its next sync.
    await s.fetch(f"UPDATE {row[0]} SET deleted = true WHERE id = %s", (row[1],))
    await update.message.reply_text(
        f"Removed the last {'workout' if row[0] == 'workouts' else 'reading'} entry. "
        "Check-in levels stay as they are; adjust with /checkin if needed.")


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = store(ctx)
    st, w = await s.streaks(), await s.week(0)
    lines = ["Streaks (current / best)"]
    lines += [f"{NAMES[h]}: {st[h]['current']} / {st[h]['best']}" for h in ("move", "read", "phone", "all")]
    if w:
        lines += ["", "This week",
                  f"Move {w['move_days']}  Read {w['read_days']}  Phone {w['phone_days']}",
                  f"{plural(w['workouts'], 'workout')}, {w['pages']} pages, screen avg {fmt_min(w['avg_screen_min'])}"]
    await update.message.reply_text("\n".join(lines))


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/checkin  today's check-in (y = yesterday)\n"
        "/workout ride 45 18.5  kind, minutes, km\n"
        "/read 25  pages, or /read 30m\n"
        "/screen 4h10  phone total (driving subtracted)\n"
        "/book 320 Title  start a book, /book done\n"
        "/stats  streaks and this week\n"
        "/undo  remove last workout or reading")


async def cmd_whoami(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Chat id: {update.effective_chat.id}")


# ---------------------------------------------------------------- scheduled

async def job_checkin(ctx: ContextTypes.DEFAULT_TYPE):
    s = store(ctx)
    if not await notify.checkin(s):
        return  # already logged, stay quiet
    text, kb = await render_checkin(s, today())
    await ctx.bot.send_message(CHAT_ID, text, reply_markup=kb)


async def job_simple(ctx: ContextTypes.DEFAULT_TYPE):
    """nudge / training / review: one message, or nothing."""
    n = await notify.KINDS[ctx.job.name](store(ctx))
    if n:
        text = f"{n['title']}\n{n['text']}" if ctx.job.name == "review" else n["text"]
        await ctx.bot.send_message(CHAT_ID, text, disable_notification=n["silent"])


# ---------------------------------------------------------------- main

async def post_init(app: Application):
    s = Store(DSN)
    await s.open()
    app.bot_data["store"] = s
    if API_TOKEN:
        app.bot_data["api"] = await api.start(s, API_TOKEN, API_PORT)
        log.info("API listening on :%s", API_PORT)
    else:
        log.warning("API_TOKEN not set: HTTP API disabled")
    if CHAT_ID and NOTIFY_TELEGRAM:
        jq = app.job_queue
        jq.run_daily(job_simple, hhmm("goals"), name="goals")
        jq.run_daily(job_simple, hhmm("move"), name="move")
        jq.run_daily(job_simple, hhmm("read"), name="read")
        jq.run_daily(job_checkin, CHECKIN_AT, name="checkin")
        jq.run_daily(job_simple, NUDGE_AT, name="nudge")
        jq.run_daily(job_simple, TRAINING_CHECK_AT, days=(6,), name="training")  # 6 = Saturday
        jq.run_daily(job_simple, REVIEW_AT, days=(0,), name="review")              # 0 = Sunday
    elif CHAT_ID:
        log.info("NOTIFY_TELEGRAM=false: scheduled messages only via the app")
    else:
        log.warning("TELEGRAM_CHAT_ID not set: send /whoami to the bot and put the id in .env")
    await app.bot.set_my_commands([
        ("checkin", "Today's check-in"), ("workout", "Log a workout"), ("read", "Log reading"),
        ("screen", "Log screen time"), ("book", "Start or finish a book"),
        ("stats", "Streaks and this week"), ("undo", "Remove last entry"), ("help", "Commands"),
    ])


async def post_shutdown(app: Application):
    if "api" in app.bot_data:
        await app.bot_data["api"].cleanup()
    await app.bot_data["store"].close()


async def api_only():
    """No Telegram token: run just the HTTP API (what the phone app syncs with)."""
    import asyncio
    s = Store(DSN)
    await s.open()
    if not API_TOKEN:
        raise SystemExit("Set TELEGRAM_TOKEN and/or API_TOKEN; with neither there is nothing to run.")
    await api.start(s, API_TOKEN, API_PORT)
    log.info("API listening on :%s (no TELEGRAM_TOKEN: bot disabled)", API_PORT)
    await asyncio.Event().wait()


def main():
    if not TOKEN:
        import asyncio
        asyncio.run(api_only())
        return
    app = (Application.builder().token(TOKEN)
           .post_init(post_init).post_shutdown(post_shutdown).build())
    only_me = filters.Chat(CHAT_ID) if CHAT_ID else filters.Chat([])
    for name, fn in [("checkin", cmd_checkin), ("screen", cmd_screen), ("workout", cmd_workout),
                     ("read", cmd_read), ("book", cmd_book), ("undo", cmd_undo),
                     ("stats", cmd_stats), ("help", cmd_help), ("start", cmd_help)]:
        app.add_handler(CommandHandler(name, fn, filters=only_me))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^c\|"))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
