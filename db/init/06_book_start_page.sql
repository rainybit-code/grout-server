-- Books you were already reading: pages before tracking count toward progress,
-- not toward pages read per day/week.
-- Existing databases: docker compose exec -T db psql -U habits -d habits < db/init/06_book_start_page.sql
ALTER TABLE books ADD COLUMN IF NOT EXISTS start_page integer NOT NULL DEFAULT 0 CHECK (start_page >= 0);

CREATE OR REPLACE VIEW v_books AS
SELECT b.id, b.title, b.total_pages, b.started, b.finished,
       (b.start_page + COALESCE(sum(r.pages), 0))::int AS pages_read,
       CASE WHEN b.total_pages > 0
            THEN LEAST(100, round(100.0 * (b.start_page + COALESCE(sum(r.pages), 0)) / b.total_pages))
       END AS pct,
       b.start_page
FROM books b
LEFT JOIN reading r ON r.book_id = b.id
GROUP BY b.id;
