-- Habit tracker schema. One row per day for the check-in, plus detail tables.
-- Levels: 0 = missed, 1 = floor (the minimum), 2 = full (a real session / 30 min reading).

CREATE TABLE checkins (
    day         date PRIMARY KEY,
    move_lvl    smallint CHECK (move_lvl BETWEEN 0 AND 2),
    read_lvl    smallint CHECK (read_lvl BETWEEN 0 AND 2),
    phone_out   boolean,                       -- phone out of the bedroom tonight
    screen_min  integer  CHECK (screen_min >= 0),  -- raw screen time from the phone
    nav_min     integer  CHECK (nav_min >= 0),     -- of which navigation while driving
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE workouts (
    id           bigserial PRIMARY KEY,
    day          date    NOT NULL,
    kind         text    NOT NULL,            -- ride, run, gym, walk, ...
    minutes      integer NOT NULL CHECK (minutes > 0),
    distance_km  numeric(6,2),
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON workouts (day);

CREATE TABLE books (
    id           serial PRIMARY KEY,
    title        text NOT NULL,
    total_pages  integer,
    started      date NOT NULL DEFAULT current_date,
    finished     date
);

CREATE TABLE reading (
    id          bigserial PRIMARY KEY,
    day         date NOT NULL,
    pages       integer CHECK (pages >= 0),
    minutes     integer CHECK (minutes >= 0),
    book_id     integer REFERENCES books(id),
    created_at  timestamptz NOT NULL DEFAULT now(),
    CHECK (pages IS NOT NULL OR minutes IS NOT NULL)
);
CREATE INDEX ON reading (day);

-- Targets live in the database so Grafana and the bot read the same numbers.
CREATE TABLE targets (
    key    text PRIMARY KEY,
    value  numeric NOT NULL
);
-- Screen target is net of navigation. Baseline net is ~3h; start at 3h, lower it at reviews.
INSERT INTO targets VALUES
    ('screen_min_per_day', 180),
    ('drive_nav_min', 60),
    ('workouts_per_week',  3),
    ('read_days_per_week', 5),
    ('phone_days_per_week', 5);
