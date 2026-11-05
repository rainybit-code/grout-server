-- Your own habits (Grout app 1.1+). The four built-in habits keep their columns in
-- checkins; this adds a list of habits (built-ins switched on/off, plus your own) and a
-- daily log for your own habits. Both sync with the phone (POST /sync). Safe to re-run.
-- Existing databases: ./scripts/migrate.sh

CREATE TABLE IF NOT EXISTS habits (
    id           text PRIMARY KEY,               -- "goals"/"move"/"read"/"phone" for built-ins, a uuid otherwise
    builtin      text,
    name         text NOT NULL,
    emoji        text NOT NULL,
    hint         text,
    kind         text NOT NULL DEFAULT 'yes_no' CHECK (kind IN ('yes_no', 'levels')),
    floor_label  text,                           -- levels: the tiny version ("1 page")
    full_label   text,                           -- levels: the full version ("a chapter")
    position     integer NOT NULL DEFAULT 0,
    enabled      boolean NOT NULL DEFAULT true,
    remind       text,                           -- "HH:MM" or null
    remind_days  smallint NOT NULL DEFAULT 127,  -- bit 0 = Monday
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    deleted      boolean NOT NULL DEFAULT false,
    rev          bigint
);

-- One row per habit and day. lvl: 0 = missed, 1 = done (yes/no) or the tiny version, 2 = full.
CREATE TABLE IF NOT EXISTS habit_logs (
    id          text PRIMARY KEY,                -- "<habit id>|<day>"
    habit_id    text NOT NULL,
    day         date NOT NULL,
    lvl         smallint CHECK (lvl BETWEEN 0 AND 2),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    rev         bigint
);
CREATE INDEX IF NOT EXISTS habit_logs_day ON habit_logs (day);

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['habits', 'habit_logs'] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS %I_sync ON %I', t, t);
        EXECUTE format('CREATE TRIGGER %I_sync BEFORE INSERT OR UPDATE ON %I
                        FOR EACH ROW EXECUTE FUNCTION sync_touch()', t, t);
        EXECUTE format('CREATE INDEX IF NOT EXISTS %I_rev ON %I (rev)', t, t);
    END LOOP;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafana_ro') THEN
        GRANT SELECT ON habits, habit_logs TO grafana_ro;
    END IF;
END $$;

-- Your own habits per day, for Grafana: done = lvl >= 1.
CREATE OR REPLACE VIEW v_habit_daily AS
SELECT l.day, h.id AS habit_id, h.name, h.emoji, l.lvl, COALESCE(l.lvl, 0) >= 1 AS done
FROM habit_logs l
JOIN habits h ON h.id = l.habit_id
WHERE NOT h.deleted;
