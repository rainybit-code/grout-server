-- Two-way sync with the phone app (POST /sync).
-- Every row gets a stable id (uuid), an updated_at for last-write-wins, a soft-delete flag
-- (so deletions reach the other side) and a server-side change counter (rev) the phone
-- uses as its cursor. Safe to re-run.
-- Existing databases: docker compose exec -T db psql -U habits -d habits < db/init/07_sync.sql

CREATE SEQUENCE IF NOT EXISTS sync_rev;

ALTER TABLE checkins ADD COLUMN IF NOT EXISTS rev bigint;
ALTER TABLE targets  ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now(),
                     ADD COLUMN IF NOT EXISTS rev bigint;
ALTER TABLE workouts ADD COLUMN IF NOT EXISTS uuid uuid NOT NULL DEFAULT gen_random_uuid(),
                     ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now(),
                     ADD COLUMN IF NOT EXISTS deleted boolean NOT NULL DEFAULT false,
                     ADD COLUMN IF NOT EXISTS rev bigint;
ALTER TABLE books    ADD COLUMN IF NOT EXISTS uuid uuid NOT NULL DEFAULT gen_random_uuid(),
                     ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now(),
                     ADD COLUMN IF NOT EXISTS deleted boolean NOT NULL DEFAULT false,
                     ADD COLUMN IF NOT EXISTS rev bigint;
ALTER TABLE reading  ADD COLUMN IF NOT EXISTS uuid uuid NOT NULL DEFAULT gen_random_uuid(),
                     ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now(),
                     ADD COLUMN IF NOT EXISTS deleted boolean NOT NULL DEFAULT false,
                     ADD COLUMN IF NOT EXISTS rev bigint;

CREATE UNIQUE INDEX IF NOT EXISTS workouts_uuid ON workouts (uuid);
CREATE UNIQUE INDEX IF NOT EXISTS books_uuid    ON books (uuid);
CREATE UNIQUE INDEX IF NOT EXISTS reading_uuid  ON reading (uuid);

-- Backfill before the trigger exists, so existing updated_at values stay as they are.
UPDATE checkins SET rev = nextval('sync_rev') WHERE rev IS NULL;
UPDATE targets  SET rev = nextval('sync_rev') WHERE rev IS NULL;
UPDATE workouts SET rev = nextval('sync_rev') WHERE rev IS NULL;
UPDATE books    SET rev = nextval('sync_rev') WHERE rev IS NULL;
UPDATE reading  SET rev = nextval('sync_rev') WHERE rev IS NULL;

-- Any write bumps rev; a write that doesn't set updated_at itself (the bot) gets now().
CREATE OR REPLACE FUNCTION sync_touch() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.updated_at IS NOT DISTINCT FROM OLD.updated_at THEN
        NEW.updated_at := now();
    END IF;
    NEW.rev := nextval('sync_rev');
    RETURN NEW;
END $$ LANGUAGE plpgsql;

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['checkins', 'targets', 'workouts', 'books', 'reading'] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS %I_sync ON %I', t, t);
        EXECUTE format('CREATE TRIGGER %I_sync BEFORE INSERT OR UPDATE ON %I
                        FOR EACH ROW EXECUTE FUNCTION sync_touch()', t, t);
        EXECUTE format('CREATE INDEX IF NOT EXISTS %I_rev ON %I (rev)', t, t);
    END LOOP;
END $$;

-- Views ignore soft-deleted rows.
CREATE OR REPLACE VIEW v_daily AS
WITH bounds AS (
    SELECT LEAST((SELECT min(day) FROM checkins),
                 (SELECT min(day) FROM workouts WHERE NOT deleted),
                 (SELECT min(day) FROM reading WHERE NOT deleted),
                 current_date) AS d0
), days AS (
    SELECT generate_series(d0, current_date, interval '1 day')::date AS day FROM bounds
), w AS (
    SELECT day, sum(minutes)::int AS minutes, count(*)::int AS n,
           COALESCE(sum(distance_km), 0) AS km
    FROM workouts WHERE NOT deleted GROUP BY day
), r AS (
    SELECT day, COALESCE(sum(pages), 0)::int AS pages, COALESCE(sum(minutes), 0)::int AS minutes
    FROM reading WHERE NOT deleted GROUP BY day
), start AS (
    SELECT LEAST((SELECT min(day) FROM checkins
                  WHERE move_lvl IS NOT NULL OR read_lvl IS NOT NULL OR phone_out IS NOT NULL),
                 (SELECT min(day) FROM workouts WHERE NOT deleted),
                 (SELECT min(day) FROM reading WHERE NOT deleted)) AS d
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

CREATE OR REPLACE VIEW v_books AS
SELECT b.id, b.title, b.total_pages, b.started, b.finished,
       (b.start_page + COALESCE(sum(r.pages), 0))::int AS pages_read,
       CASE WHEN b.total_pages > 0
            THEN LEAST(100, round(100.0 * (b.start_page + COALESCE(sum(r.pages), 0)) / b.total_pages))
       END AS pct,
       b.start_page
FROM books b
LEFT JOIN reading r ON r.book_id = b.id AND NOT r.deleted
WHERE NOT b.deleted
GROUP BY b.id;
