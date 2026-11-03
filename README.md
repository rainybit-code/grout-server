# Grout server

The optional, self-hosted server for **[Grout](https://grout.rainybit.dk)**, the Android app for small daily habits and less screen time.

The app works fine on its own. Run this server if you want:

- **your data on a machine you control**, synced both ways with the app
- **a Grafana dashboard** of streaks, screen time, training and books ([docs/grafana.md](docs/grafana.md))
- **a Telegram bot** for logging and reminders (optional)
- **a small HTTP API** for shortcuts, scripts or a Strava webhook

```
 Grout app ──┐                          ┌── Grafana (read-only user)
 Telegram ───┼── app (API + bot) ── Postgres
 curl/HTTP ──┘   behind your HTTPS proxy
```

It's two containers: Postgres 17, and one small Python service (aiohttp, python-telegram-bot, psycopg).

---

## Host it

You need a machine with Docker and Docker Compose, a domain name, and a reverse proxy that does HTTPS: Caddy, nginx or Traefik. The app only talks to `https://` addresses.

### 1. Get the code and configure

```sh
git clone https://github.com/rainybit-code/grout-server.git
cd grout-server
cp .env.example .env
```

Fill in `.env`:

| Variable | What |
|---|---|
| `DB_PASSWORD` | Postgres password. `openssl rand -hex 24` |
| `GRAFANA_DB_PASSWORD` | Password for the read-only `grafana_ro` user. `openssl rand -hex 24` |
| `API_TOKEN` | The token you enter in the app. `openssl rand -hex 24`. Empty turns the API off |
| `TZ` | Your timezone, e.g. `Europe/Copenhagen`. Decides what "today" is. Days roll over at 04:00 |
| `TELEGRAM_TOKEN` | Optional, see [Telegram bot](#telegram-bot-optional). Empty = API only |

The rest is optional and documented in `.env.example`.

### 2. Start

```sh
docker compose up -d --build
docker compose logs -f app        # "API listening on :8080"
curl http://127.0.0.1:8087/health # {"ok": true}
```

On first start, the scripts in `db/init/` create the tables, views and the `grafana_ro` user.

The API listens on **127.0.0.1:8087** only (change it with `API_HOST_PORT`). Postgres is on **127.0.0.1:5433**, for Grafana. Neither is reachable from outside the machine.

### 3. HTTPS

Point a domain, e.g. `habits.example.com`, at the machine and proxy it to `127.0.0.1:8087`.

**Caddy** (gets the certificate itself):

```
habits.example.com {
    reverse_proxy 127.0.0.1:8087
}
```

**nginx** (with certbot or another certificate):

```nginx
server {
    listen 443 ssl;
    http2 on;
    server_name habits.example.com;
    ssl_certificate     /etc/letsencrypt/live/habits.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/habits.example.com/privkey.pem;
    client_max_body_size 1m;

    location / {
        proxy_pass http://127.0.0.1:8087;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
server { listen 80; server_name habits.example.com; return 301 https://$host$request_uri; }
```

If nginx runs in Docker itself, `127.0.0.1` is the nginx container, not the host. Either put both on one Docker network (see `docker-compose.override.yml.example`) and use `proxy_pass http://grout-app:8080;`, or use the host's address.

**Traefik:** add labels to the `app` service in a `docker-compose.override.yml`, routing your host to port 8080.

Check it from outside:

```sh
curl https://habits.example.com/health                                  # {"ok": true}
curl -H "Authorization: Bearer $API_TOKEN" https://habits.example.com/stats
```

### 4. Connect the app

In Grout: **Settings → Server sync (optional)**. Enter `https://habits.example.com` and the `API_TOKEN`, then tap **Connect and sync**. Step by step, with token handling and troubleshooting: **[docs/app.md](docs/app.md)**.

- Everything already on the phone is uploaded, and everything on the server comes down.
- After that the app syncs after every change, when it's opened, and every hour.
- Several phones can use the same server. They share one set of data, because this server holds one person's habits.

---

## Telegram bot (optional)

1. In Telegram, message **@BotFather**, send `/newbot`, and copy the token into `TELEGRAM_TOKEN`.
2. Run `docker compose up -d`, then send `/whoami` to your bot.
3. Put the number it replies with into `TELEGRAM_CHAT_ID`, and run `docker compose up -d` again. Until then the bot answers nobody.

| Command | |
|---|---|
| `/checkin` | today's check-in buttons (add `y` for yesterday) |
| `/workout ride 45 18` | kind, minutes, km (`1h05` works too) |
| `/read 25` / `/read 30m` | pages / minutes |
| `/screen 4h10` | phone total; `drive 40` or `nodrive` to override driving |
| `/book 320 Title p75` | start a book (already on page 75); `/book done` |
| `/stats`, `/undo`, `/whoami` | |

**Scheduled messages:** the morning goals reminder, move and read reminders, the evening check-in, a nudge when a 3+ day streak is open, a Saturday training check, and a Sunday summary. Their times are set with `*_TIME` in `.env`. If you use the app for reminders, set `NOTIFY_TELEGRAM=false` so you don't get them twice.

---

## Grafana

See **[docs/grafana.md](docs/grafana.md)**: datasource, dashboard import, Grafana on the same machine or elsewhere.

---

## HTTP API

All endpoints except `/health` need `Authorization: Bearer $API_TOKEN`. Days are `YYYY-MM-DD` in `TZ`, defaulting to today.

```sh
H="Authorization: Bearer $API_TOKEN"; U=https://habits.example.com
curl -H "$H" $U/stats
curl -H "$H" "$U/dashboard?days=35"                 # what the app shows
curl -H "$H" $U/notify/checkin                      # what a reminder would say now, or null
curl -H "$H" -X POST $U/checkin -d '{"goals":true,"move_lvl":1,"read_lvl":2,"phone_out":true}'
curl -H "$H" -X POST $U/checkin -d '{"screen":"4h10","nav":"45m"}'
curl -H "$H" -X POST $U/workout -d '{"kind":"ride","duration":"1h05","km":21.5}'
curl -H "$H" -X POST $U/reading -d '{"pages":25}'
curl -H "$H" -X POST $U/book -d '{"title":"Dune","total_pages":600,"current_page":75}'
curl -H "$H" -X POST $U/book/page -d '{"page":110}'     # logs the 35 pages since last time
curl -H "$H" -X POST $U/book/done
curl -H "$H" -X POST $U/sync -d '{"since":0}'            # everything, as the app pulls it
```

Levels: `0` missed, `1` the minimum (2 min move / 10 pages), `2` full (a workout / 30 pages).

### Sync protocol

- **Rows:** every row has a uuid (check-ins are keyed by day), `updated_at`, and a soft-delete flag.
- **Server stamps:** every write gets a `rev` number from a sequence, set by triggers in `db/init/07_sync.sql`.
- **Exchange:** the app sends `{"since": <highest rev it has>, "changes": {table: [rows]}}`. It gets back `{"cursor": <new highest rev>, "changes": {...}}` with every row changed since then, deletions included.
- **Conflicts:**
  - Books, workouts and reading: the newest `updated_at` wins.
  - Check-ins merge per field. The app only sends the fields it changed, so its hourly screen-time update never overwrites a check-in made through the bot.

---

## Day to day

**Backups.** Your data is a single Postgres database:

```sh
mkdir -p backups
docker compose exec -T db pg_dump -U habits habits | gzip > backups/habits-$(date +%F).sql.gz
```

For a nightly backup, add this to `crontab -e`, adjusting the path:

```
15 3 * * * cd /srv/grout-server && docker compose exec -T db pg_dump -U habits habits | gzip > backups/habits-$(date +\%F).sql.gz && find backups -name '*.sql.gz' -mtime +14 -delete
```

Restore into an empty database: `gunzip -c backups/habits-DATE.sql.gz | docker compose exec -T db psql -U habits habits`.

**Upgrading.**

```sh
git pull
./scripts/migrate.sh          # applies new database changes; safe to run any time
docker compose up -d --build
```

The `db/init/` scripts only run automatically on a fresh database. Scripts from `05_` on are written so they can be re-applied, which is what `migrate.sh` does.

**Targets** (screen time per day, workouts per week, reading and phone days per week) can be set in the app, or here:

```sh
docker compose exec db psql -U habits -c "UPDATE targets SET value = 150 WHERE key = 'screen_min_per_day'"
```

## Security notes

- **Exposure:** only the HTTPS proxy should be reachable from the internet. Keep Postgres on `127.0.0.1` or a VPN address (`PG_BIND`), never `0.0.0.0`.
- **The token:** `API_TOKEN` gives full read and write access to your habit data. Use a long random one, and change it if it leaks: edit `.env`, run `docker compose up -d`, and enter the new token in the app.
- **Rate limiting:** worth adding at the proxy, e.g. nginx `limit_req`.
- **Telegram:** the bot only obeys `TELEGRAM_CHAT_ID`.

## License

MIT, see [LICENSE](LICENSE).
