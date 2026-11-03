-- Stats views. Grafana and the bot only read these.
-- Postgres runs with timezone=$TZ (docker-compose.yml), so current_date is local.

-- One row per calendar day since the first log, gaps included (a gap is a miss).
CREATE VIEW v_daily AS
WITH bounds AS (
    SELECT LEAST((SELECT min(day) FROM checkins),
                 (SELECT min(day) FROM workouts),
                 (SELECT min(day) FROM reading),
                 current_date) AS d0
), days AS (
    SELECT generate_series(d0, current_date, interval '1 day')::date AS day FROM bounds
), w AS (
    SELECT day, sum(minutes)::int AS minutes, count(*)::int AS n,
           COALESCE(sum(distance_km), 0) AS km
    FROM workouts GROUP BY day
), r AS (
    SELECT day, COALESCE(sum(pages), 0)::int AS pages, COALESCE(sum(minutes), 0)::int AS minutes
    FROM reading GROUP BY day
), start AS (
    -- Habit tracking starts with the first habit entry; earlier days (the
    -- screen-time baseline) are shown but not counted as misses.
    SELECT LEAST((SELECT min(day) FROM checkins
                  WHERE move_lvl IS NOT NULL OR read_lvl IS NOT NULL OR phone_out IS NOT NULL),
                 (SELECT min(day) FROM workouts),
                 (SELECT min(day) FROM reading)) AS d
)
SELECT d.day,
       d.day >= COALESCE((SELECT d FROM start), current_date) AS tracking,
       c.move_lvl, c.read_lvl, c.phone_out,
       c.screen_min,
       c.nav_min,
       CASE WHEN c.screen_min IS NOT NULL
            THEN GREATEST(c.screen_min - COALESCE(c.nav_min, 0), 0) END AS screen_net_min,
       COALESCE(w.minutes, 0) AS workout_min,
       COALESCE(w.n, 0)       AS workouts,
       COALESCE(w.km, 0)      AS km,
       COALESCE(r.pages, 0)   AS pages,
       COALESCE(r.minutes, 0) AS read_min,
       COALESCE(c.move_lvl, 0) >= 1          AS move_hit,
       COALESCE(c.read_lvl, 0) >= 1          AS read_hit,
       COALESCE(c.phone_out, false)          AS phone_hit,
       (COALESCE(c.move_lvl, 0) >= 1 AND COALESCE(c.read_lvl, 0) >= 1
        AND COALESCE(c.phone_out, false))    AS all_hit,
       c.day IS NOT NULL                     AS logged
FROM days d
LEFT JOIN checkins c USING (day)
LEFT JOIN w USING (day)
LEFT JOIN r USING (day);

-- Long format: one row per day per habit. Handy for heatmaps and streaks.
CREATE VIEW v_habit_days AS
SELECT day, 'move'  AS habit, move_hit  AS hit FROM v_daily
UNION ALL SELECT day, 'read',  read_hit  FROM v_daily
UNION ALL SELECT day, 'phone', phone_hit FROM v_daily
UNION ALL SELECT day, 'all',   all_hit   FROM v_daily;

-- Every run of consecutive hit days (gaps-and-islands).
CREATE VIEW v_runs AS
WITH g AS (
    SELECT habit, day,
           day - (row_number() OVER (PARTITION BY habit ORDER BY day))::int AS grp
    FROM v_habit_days WHERE hit
)
SELECT habit, min(day) AS start_day, max(day) AS end_day, count(*)::int AS len
FROM g GROUP BY habit, grp;

-- Current and best streak per habit. Today not logged yet does not break the
-- streak: a run ending yesterday still counts as current.
CREATE VIEW v_streaks AS
SELECT h.habit,
       COALESCE((SELECT len FROM v_runs r
                 WHERE r.habit = h.habit AND r.end_day >= current_date - 1
                 ORDER BY end_day DESC LIMIT 1), 0) AS current_streak,
       COALESCE((SELECT max(len) FROM v_runs r WHERE r.habit = h.habit), 0) AS best_streak,
       EXISTS (SELECT 1 FROM v_habit_days d
               WHERE d.habit = h.habit AND d.day = current_date AND d.hit) AS done_today
FROM (VALUES ('move'), ('read'), ('phone'), ('all')) AS h(habit);

-- Weekly rollup (ISO weeks, Monday start).
CREATE VIEW v_weekly AS
SELECT date_trunc('week', day)::date                AS week,
       count(*) FILTER (WHERE tracking)::int        AS days,
       count(*) FILTER (WHERE move_hit)::int        AS move_days,
       count(*) FILTER (WHERE read_hit)::int        AS read_days,
       count(*) FILTER (WHERE phone_hit)::int       AS phone_days,
       count(*) FILTER (WHERE all_hit)::int         AS all_days,
       sum(workouts)::int                           AS workouts,
       sum(workout_min)::int                        AS workout_min,
       sum(km)                                      AS km,
       sum(pages)::int                              AS pages,
       round(avg(screen_net_min))::int              AS avg_screen_min,   -- net of navigation
       round(avg(screen_min))::int                  AS avg_screen_raw
FROM v_daily
GROUP BY 1;

-- Books with pages read so far.
CREATE VIEW v_books AS
SELECT b.id, b.title, b.total_pages, b.started, b.finished,
       COALESCE(sum(r.pages), 0)::int AS pages_read,
       CASE WHEN b.total_pages > 0
            THEN LEAST(100, round(100.0 * COALESCE(sum(r.pages), 0) / b.total_pages))
       END AS pct
FROM books b
LEFT JOIN reading r ON r.book_id = b.id
GROUP BY b.id;
