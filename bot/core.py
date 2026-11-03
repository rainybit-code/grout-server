"""Database logic shared by the Telegram bot and the HTTP API."""

import datetime as dt
import os
import re

from psycopg_pool import AsyncConnectionPool
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.environ.get("TZ_NAME", "UTC"))
ROLLOVER_HOUR = 4  # logging at 01:00 still counts for the day before
MILESTONES = {7, 14, 21, 30, 50, 75, 100, 150, 200, 365}
NAMES = {"move": "Move", "read": "Read", "phone": "Phone out", "all": "All three"}
CHECKIN_COLS = ("move_lvl", "read_lvl", "phone_out", "goals", "screen_min", "nav_min")
# Days you drive to work. On these, screen time is logged minus the usual navigation time.
DRIVE_DAYS = {d.strip().lower()[:3] for d in os.environ.get("DRIVE_DAYS", "").split(",") if d.strip()}


def today() -> dt.date:
    now = dt.datetime.now(TZ)
    if now.hour < ROLLOVER_HOUR:
        now -= dt.timedelta(days=1)
    return now.date()


def parse_duration(s: str) -> int | None:
    """'1h20', '1h 20m', '80', '45m', '2h' -> minutes."""
    m = re.fullmatch(r"\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m?)?\s*", str(s).lower())
    if not m or (m.group(1) is None and m.group(2) is None):
        return None
    return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)


def fmt_min(n) -> str:
    if n is None:
        return "-"
    n = int(n)
    sign, n = ("-", -n) if n < 0 else ("", n)
    return sign + (f"{n // 60}h{n % 60:02d}" if n >= 60 else f"{n} min")


def plural(n, word) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def day_label(day: dt.date) -> str:
    return "Today" if day == today() else day.strftime("%a %d %b")


class Store:
    def __init__(self, dsn: str):
        self.pool = AsyncConnectionPool(dsn, min_size=1, max_size=4, open=False,
                                        kwargs={"autocommit": True})
        self.milestones_sent: set = set()

    async def open(self):
        await self.pool.open(wait=True)

    async def close(self):
        await self.pool.close()

    async def fetch(self, sql, params=()):
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return await cur.fetchall() if cur.description else []

    async def fetchone(self, sql, params=()):
        rows = await self.fetch(sql, params)
        return rows[0] if rows else None

    async def target(self, key, default):
        row = await self.fetchone("SELECT value FROM targets WHERE key = %s", (key,))
        return float(row[0]) if row else default

    # ---------------------------------------------------------- writes

    async def set_checkin(self, day: dt.date, **fields):
        cols = [c for c in fields if c in CHECKIN_COLS]
        if not cols:
            return
        await self.fetch(f"""
            INSERT INTO checkins (day, {', '.join(cols)}) VALUES (%s{', %s' * len(cols)})
            ON CONFLICT (day) DO UPDATE SET {', '.join(f'{c} = EXCLUDED.{c}' for c in cols)},
                                            updated_at = now()""",
            (day, *[fields[c] for c in cols]))

    async def log_screen(self, day, screen_min: int, nav_min: int | None = None) -> dict:
        """Raw screen time; navigation defaults to the usual drive on drive days, else 0."""
        if nav_min is None:
            is_drive_day = day.strftime("%a").lower() in DRIVE_DAYS
            nav_min = int(await self.target("drive_nav_min", 60)) if is_drive_day else 0
        nav_min = min(nav_min, screen_min)
        await self.set_checkin(day, screen_min=screen_min, nav_min=nav_min)
        avg = await self.fetchone("""SELECT round(avg(screen_net_min)) FROM v_daily
                                     WHERE day > %s::date - 7 AND day <= %s AND screen_min IS NOT NULL""",
                                  (day, day))
        return {"day": day, "screen_min": screen_min, "nav_min": nav_min,
                "net_min": screen_min - nav_min,
                "avg7_net_min": int(avg[0]) if avg and avg[0] is not None else None,
                "target_min": int(await self.target("screen_min_per_day", 180))}

    async def bump_level(self, day, col, lvl):
        """Raise move/read level, never lower it (a workout implies 'session')."""
        assert col in ("move_lvl", "read_lvl")
        await self.fetch(f"""
            INSERT INTO checkins (day, {col}) VALUES (%s, %s)
            ON CONFLICT (day) DO UPDATE
            SET {col} = GREATEST(COALESCE(checkins.{col}, 0), EXCLUDED.{col}), updated_at = now()""",
            (day, lvl))

    async def log_workout(self, day, kind, minutes, km=None) -> dict:
        await self.fetch("INSERT INTO workouts (day, kind, minutes, distance_km) VALUES (%s,%s,%s,%s)",
                         (day, kind.lower(), minutes, km))
        await self.bump_level(day, "move_lvl", 2)
        wk = await self.fetchone("SELECT workouts FROM v_weekly WHERE week = date_trunc('week', %s::date)", (day,))
        return {"day": day, "kind": kind.lower(), "minutes": minutes, "km": km,
                "week_workouts": wk[0] if wk else 1,
                "week_target": int(await self.target("workouts_per_week", 3)),
                "milestones": await self.milestones(day)}

    async def log_reading(self, day, pages=None, minutes=None) -> dict:
        book = await self.fetchone("""SELECT id, title FROM books WHERE finished IS NULL AND NOT deleted
                                      ORDER BY started DESC, id DESC LIMIT 1""")
        await self.fetch("INSERT INTO reading (day, pages, minutes, book_id) VALUES (%s,%s,%s,%s)",
                         (day, pages, minutes, book[0] if book else None))
        p, m = await self.fetchone("SELECT pages, read_min FROM v_daily WHERE day = %s", (day,)) or (0, 0)
        lvl = 2 if (p >= 30 or m >= 30) else 1 if (p >= 10 or m >= 10) else 0
        if lvl:
            await self.bump_level(day, "read_lvl", lvl)
        out = {"day": day, "day_pages": p, "day_minutes": m, "book": None}
        if book:
            b = await self.fetchone("SELECT pages_read, total_pages, pct FROM v_books WHERE id = %s", (book[0],))
            out["book"] = {"title": book[1], "pages_read": b[0], "total_pages": b[1],
                           "pct": float(b[2]) if b[2] is not None else None}
        out["milestones"] = await self.milestones(day)
        return out

    # ---------------------------------------------------------- books

    async def current_book(self) -> dict | None:
        row = await self.fetchone("""SELECT id, title, total_pages, started, pages_read, pct, start_page FROM v_books
                                     WHERE finished IS NULL ORDER BY started DESC, id DESC LIMIT 1""")
        if not row:
            return None
        b = dict(zip(("id", "title", "total_pages", "started", "pages_read", "pct", "start_page"), row))
        days = max(1, (today() - b["started"]).days + 1)
        b["pages_per_day"] = round((b["pages_read"] - b["start_page"]) / days, 1)   # since tracking began
        left = (b["total_pages"] or 0) - b["pages_read"]
        b["finish_estimate"] = (today() + dt.timedelta(days=round(left / b["pages_per_day"]))
                                if left > 0 and b["pages_per_day"] > 0 else None)
        return b

    async def start_book(self, title: str, total_pages: int, start_page: int = 0) -> dict:
        """start_page: already read before tracking (counts for progress, not as reading done)."""
        await self.fetch("INSERT INTO books (title, total_pages, started, start_page) VALUES (%s,%s,%s,%s)",
                         (title, total_pages, today(), start_page))
        return await self.current_book()

    async def finish_book(self) -> dict | None:
        row = await self.fetchone("""UPDATE books SET finished = %s WHERE id = (
                                       SELECT id FROM books WHERE finished IS NULL AND NOT deleted
                                       ORDER BY started DESC, id DESC LIMIT 1)
                                     RETURNING title""", (today(),))
        if not row:
            return None
        n = await self.fetchone("SELECT count(*) FROM books WHERE finished >= date_trunc('year', current_date) "
                                "AND NOT deleted")
        return {"title": row[0], "finished_this_year": n[0]}

    # ---------------------------------------------------------- goals (yes/no in checkins)

    async def goals_streak(self) -> dict:
        """Days in a row with goals = yes; today not answered yet doesn't break it."""
        rows = await self.fetch("SELECT day FROM checkins WHERE goals ORDER BY day")
        days = [r[0] for r in rows]
        best = run = 0
        prev = None
        for d in days:
            run = run + 1 if prev and (d - prev).days == 1 else 1
            best, prev = max(best, run), d
        yes = set(days)
        d = today() if today() in yes else today() - dt.timedelta(days=1)
        cur = 0
        while d in yes:
            cur, d = cur + 1, d - dt.timedelta(days=1)
        return {"current": cur, "best": best, "done_today": today() in yes}

    # ---------------------------------------------------------- reads

    async def checkin_state(self, day) -> dict:
        row = await self.fetchone(f"SELECT {', '.join(CHECKIN_COLS)} FROM checkins WHERE day = %s", (day,))
        return dict(zip(CHECKIN_COLS, row)) if row else dict.fromkeys(CHECKIN_COLS)

    async def streaks(self) -> dict:
        rows = await self.fetch("SELECT habit, current_streak, best_streak, done_today FROM v_streaks")
        return {h: {"current": c, "best": b, "done_today": d} for h, c, b, d in rows}

    async def week(self, offset=0) -> dict | None:
        row = await self.fetchone("""
            SELECT week, days, move_days, read_days, phone_days, all_days, workouts,
                   workout_min, km, pages, avg_screen_min
            FROM v_weekly WHERE week = date_trunc('week', current_date) - %s * interval '1 week'""",
            (offset,))
        keys = ("week", "days", "move_days", "read_days", "phone_days", "all_days", "workouts",
                "workout_min", "km", "pages", "avg_screen_min")
        return dict(zip(keys, row)) if row else None

    async def milestones(self, day) -> list[str]:
        """Lines for streaks that just hit a milestone or a new best. Each fires once."""
        if day != dt.datetime.now(TZ).date():
            return []
        lines = []
        for habit, s in (await self.streaks()).items():
            n = s["current"]
            key = (day, habit, n)
            if not s["done_today"] or n < 3 or key in self.milestones_sent:
                continue
            if n in MILESTONES:
                lines.append(f"{NAMES[habit]}: {n} days in a row.")
            elif n == s["best"] and n >= 5:
                lines.append(f"{NAMES[habit]}: {n} days, a new best.")
            else:
                continue
            self.milestones_sent.add(key)
        return lines
