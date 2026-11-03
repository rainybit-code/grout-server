-- Morning checkpoint: wrote down 3 goals for the day (yes/no), part of the daily check-in.
-- Existing databases: docker compose exec -T db psql -U habits -d habits < db/init/05_goals.sql
ALTER TABLE checkins ADD COLUMN IF NOT EXISTS goals boolean;
