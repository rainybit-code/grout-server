"""What each scheduled moment says, or None to stay quiet.

Shared by the Telegram jobs (app.py) and the Android app, which asks
GET /notify/<kind> when its own alarm fires and shows the result as a notification.
"""

import os

from core import NAMES, Store, fmt_min, plural, today

# ISO weekday (Mon=1 .. Sun=7) or None for daily. Times are in TZ.
SCHEDULE = {
    "goals":    {"time": os.environ.get("GOALS_TIME", "07:00"), "weekday": None},
    "move":     {"time": os.environ.get("MOVE_REMINDER_TIME", "11:00"), "weekday": None},
    "read":     {"time": os.environ.get("READ_REMINDER_TIME", "19:20"), "weekday": None},
    "checkin":  {"time": os.environ.get("CHECKIN_TIME", "21:30"), "weekday": None},
    "nudge":    {"time": os.environ.get("NUDGE_TIME", "22:30"), "weekday": None},
    "training": {"time": os.environ.get("TRAINING_CHECK_TIME", "10:00"), "weekday": 6},
    "review":   {"time": os.environ.get("REVIEW_TIME", "19:00"), "weekday": 7},
}

CHECKIN_FIELDS = {"goals": "goals", "move_lvl": "move", "read_lvl": "read", "phone_out": "phone"}


async def checkin(s: Store) -> dict | None:
    state = await s.checkin_state(today())
    missing = [name for col, name in CHECKIN_FIELDS.items() if state[col] is None]
    if not missing:
        return None  # already logged, stay quiet
    return {"title": "Evening check-in", "text": "Tap to log " + ", ".join(missing) + ".", "silent": False}


async def goals(s: Store) -> dict | None:
    if (await s.checkin_state(today()))["goals"]:
        return None
    n = (await s.goals_streak())["current"]
    streak = f" Streak: {n} {'day' if n == 1 else 'days'}." if n else ""
    return {"title": "Today's 3 goals", "text": "Write down three goals for today, then tick it off." + streak,
            "silent": False}


async def _habit_reminder(s: Store, col: str, habit: str, title: str, how: str) -> dict | None:
    if ((await s.checkin_state(today()))[col] or 0) >= 1:
        return None  # already at the floor today
    n = (await s.streaks())[habit]["current"]
    streak = f" Streak: {n} {'day' if n == 1 else 'days'}." if n else ""
    return {"title": title, "text": how + streak, "silent": False}


async def move(s: Store) -> dict | None:
    return await _habit_reminder(s, "move_lvl", "move", "Time to move",
                                 "2 minutes is enough: a few push-ups, pull-ups or squats keeps the move streak.")


async def read(s: Store) -> dict | None:
    return await _habit_reminder(s, "read_lvl", "read", "Time to read",
                                 "10 pages or 10 minutes keeps the read streak.")


async def nudge(s: Store) -> dict | None:
    """Only when a streak worth protecting is about to break."""
    at_risk = [(h, v["current"]) for h, v in (await s.streaks()).items()
               if h != "all" and not v["done_today"] and v["current"] >= 3]
    if not at_risk:
        return None
    h, n = max(at_risk, key=lambda x: x[1])
    extra = f" (+{len(at_risk) - 1} more)" if len(at_risk) > 1 else ""
    return {"title": f"{NAMES[h]} streak: {n} days",
            "text": f"{NAMES[h]} streak is at {n} days and still open tonight{extra}.", "silent": False}


async def training(s: Store) -> dict | None:
    """Saturday: only if the weekly training target is still open."""
    w = await s.week(0)
    done, goal = (w["workouts"] if w else 0), int(await s.target("workouts_per_week", 3))
    if done >= goal:
        return None
    return {"title": "Training this week", "text": f"{done} of {goal} sessions this week, weekend left.",
            "silent": False}


async def review(s: Store) -> dict | None:
    w, prev = await s.week(0), await s.week(1)
    if not w:
        return None

    def delta(key, fmt=str):
        if not prev or prev[key] is None or w[key] is None:
            return ""
        d = w[key] - prev[key]
        return f" ({'+' if d >= 0 else ''}{fmt(d)})"

    a = (await s.streaks())["all"]
    return {"title": f"Week of {w['week']:%d %b}", "silent": True, "text": "\n".join([
        f"Move {w['move_days']}/7  Read {w['read_days']}/7  Phone {w['phone_days']}/7",
        f"Training: {plural(w['workouts'], 'session')}, {fmt_min(w['workout_min'])}{delta('workout_min', fmt_min)}",
        f"Reading: {w['pages']} pages{delta('pages')}",
        f"Screen time avg: {fmt_min(w['avg_screen_min'])}{delta('avg_screen_min', fmt_min)}",
        f"All-three streak: {a['current']} (best {a['best']})",
        "",
        "Review: what made the good days easy? Put next week's 3 sessions in the calendar.",
    ])}


KINDS = {"goals": goals, "move": move, "read": read, "checkin": checkin, "nudge": nudge, "training": training, "review": review}
