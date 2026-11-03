"""Two-way sync with the phone app: POST /sync.

The phone sends rows it changed since its last sync plus its cursor (the highest
server `rev` it has seen); the server applies them last-write-wins on updated_at and
returns every row with rev > cursor, deletions included (deleted = true).

Times on the wire are epoch milliseconds; days are YYYY-MM-DD.
"""

import datetime as dt
import decimal

from core import Store

CHECKIN_FIELDS = ("move_lvl", "read_lvl", "phone_out", "goals", "screen_min", "nav_min")


def _ts(ms) -> dt.datetime:
    return dt.datetime.fromtimestamp(int(ms) / 1000, dt.timezone.utc)


def _ms(t: dt.datetime | None) -> int | None:
    return int(t.timestamp() * 1000) if t else None


def _day(s) -> dt.date | None:
    return dt.date.fromisoformat(str(s)) if s else None


async def apply(store: Store, since: int, changes: dict) -> dict:
    async with store.pool.connection() as conn:
        async with conn.transaction():
            for r in changes.get("books", []):
                await conn.execute("""
                    INSERT INTO books (uuid, title, total_pages, start_page, started, finished, updated_at, deleted)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (uuid) DO UPDATE SET title = EXCLUDED.title, total_pages = EXCLUDED.total_pages,
                        start_page = EXCLUDED.start_page, started = EXCLUDED.started, finished = EXCLUDED.finished,
                        updated_at = EXCLUDED.updated_at, deleted = EXCLUDED.deleted
                    WHERE books.updated_at < EXCLUDED.updated_at""",
                    (r["uuid"], r["title"], r.get("total_pages"), r.get("start_page") or 0, _day(r["started"]),
                     _day(r.get("finished")), _ts(r["updated_at"]), bool(r.get("deleted"))))
            for r in changes.get("workouts", []):
                await conn.execute("""
                    INSERT INTO workouts (uuid, day, kind, minutes, distance_km, created_at, updated_at, deleted)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (uuid) DO UPDATE SET day = EXCLUDED.day, kind = EXCLUDED.kind,
                        minutes = EXCLUDED.minutes, distance_km = EXCLUDED.distance_km,
                        updated_at = EXCLUDED.updated_at, deleted = EXCLUDED.deleted
                    WHERE workouts.updated_at < EXCLUDED.updated_at""",
                    (r["uuid"], _day(r["day"]), r["kind"], r["minutes"], r.get("km"),
                     _ts(r.get("created_at") or r["updated_at"]), _ts(r["updated_at"]), bool(r.get("deleted"))))
            for r in changes.get("reading", []):
                await conn.execute("""
                    INSERT INTO reading (uuid, day, pages, minutes, book_id, created_at, updated_at, deleted)
                    VALUES (%s, %s, %s, %s, (SELECT id FROM books WHERE uuid = %s), %s, %s, %s)
                    ON CONFLICT (uuid) DO UPDATE SET day = EXCLUDED.day, pages = EXCLUDED.pages,
                        minutes = EXCLUDED.minutes, book_id = EXCLUDED.book_id,
                        updated_at = EXCLUDED.updated_at, deleted = EXCLUDED.deleted
                    WHERE reading.updated_at < EXCLUDED.updated_at""",
                    (r["uuid"], _day(r["day"]), r.get("pages"), r.get("minutes"), r.get("book_uuid"),
                     _ts(r.get("created_at") or r["updated_at"]), _ts(r["updated_at"]), bool(r.get("deleted"))))
            for r in changes.get("checkins", []):
                # Merged per field: the phone sends only the fields it changed, so a
                # check-in made through the bot isn't overwritten by an hourly screen-time push.
                fields = [f for f in CHECKIN_FIELDS if f in r]
                if not fields:
                    continue
                await conn.execute(f"""
                    INSERT INTO checkins (day, {', '.join(fields)}, updated_at)
                    VALUES (%s, {', '.join(['%s'] * len(fields))}, %s)
                    ON CONFLICT (day) DO UPDATE SET {', '.join(f'{f} = EXCLUDED.{f}' for f in fields)},
                        updated_at = GREATEST(checkins.updated_at, EXCLUDED.updated_at)""",
                    (_day(r["day"]), *[r[f] for f in fields], _ts(r["updated_at"])))
            for r in changes.get("targets", []):
                await conn.execute("""
                    INSERT INTO targets (key, value, updated_at) VALUES (%s, %s, %s)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
                    WHERE targets.updated_at < EXCLUDED.updated_at""",
                    (r["key"], r["value"], _ts(r["updated_at"])))

        out, cursor = {}, since

        async def rows(name, sql):
            nonlocal cursor
            cur = await conn.execute(sql, (since,))
            cols = [c.name for c in cur.description]
            result = []
            for row in await cur.fetchall():
                d = dict(zip(cols, row))
                cursor = max(cursor, d.pop("rev"))
                for k, v in list(d.items()):
                    if isinstance(v, dt.datetime):
                        d[k] = _ms(v)
                    elif isinstance(v, dt.date):
                        d[k] = v.isoformat()
                    elif k in ("uuid", "book_uuid") and v is not None:
                        d[k] = str(v)
                    elif isinstance(v, decimal.Decimal):
                        d[k] = float(v)
                result.append(d)
            out[name] = result

        await rows("checkins", f"SELECT day, {', '.join(CHECKIN_FIELDS)}, updated_at, rev FROM checkins WHERE rev > %s")
        await rows("targets", "SELECT key, value, updated_at, rev FROM targets WHERE rev > %s")
        await rows("books", """SELECT uuid, title, total_pages, start_page, started, finished, updated_at, deleted, rev
                               FROM books WHERE rev > %s""")
        await rows("workouts", """SELECT uuid, day, kind, minutes, distance_km AS km, created_at, updated_at, deleted, rev
                                  FROM workouts WHERE rev > %s""")
        await rows("reading", """SELECT r.uuid, r.day, r.pages, r.minutes, b.uuid AS book_uuid, r.created_at,
                                        r.updated_at, r.deleted, r.rev
                                 FROM reading r LEFT JOIN books b ON b.id = r.book_id WHERE r.rev > %s""")
    return {"cursor": cursor, "changes": out}
