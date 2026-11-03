# Grafana

`grafana/dashboards/habits.json` is a ready-made dashboard with these panels:
- current and best streaks (including goals),
- 7-day screen-time average,
- a daily habit timeline,
- screen time per day with its target line,
- days at the minimum per week, training minutes and pages per week,
- **Now reading** and **Finished books**.

Grafana reads Postgres with a **read-only** user, `grafana_ro`, which is created on first start with the password `GRAFANA_DB_PASSWORD`. It can only `SELECT`.

The dashboard expects a PostgreSQL datasource with **uid `habits-pg`**. If yours has a different uid, see [Using a different datasource uid](#using-a-different-datasource-uid).

---

## Where does Grafana run?

### A. Grafana on the same machine (not in Docker)

Postgres listens on `127.0.0.1:5433`. Use host `127.0.0.1:5433`.

### B. Grafana in Docker on the same machine

Inside a container, `127.0.0.1` is the container itself. Put Grafana and the database on one Docker network:

```yaml
# docker-compose.override.yml (next to docker-compose.yml)
services:
  db:
    networks:
      default:
      grafana:
        aliases: [grout-db]
networks:
  grafana:
    external: true
    name: grafana_default        # the network your Grafana container is on: docker network ls
```

Run `docker compose up -d`, then use host `grout-db:5432` (the container port, not 5433).

### C. Grafana on another machine (or Grafana Cloud)

**Don't publish Postgres on the internet.** Use one of these:

- **A VPN** such as Tailscale or WireGuard, with both machines on it. Set `PG_BIND=<this machine's VPN IP>` in `.env` and run `docker compose up -d`. In Grafana, use `<VPN IP>:5433`.
- **An SSH tunnel** from the Grafana machine:
  ```sh
  ssh -N -L 5433:127.0.0.1:5433 you@your-server
  ```
  Then use `127.0.0.1:5433` on the Grafana machine.
- **Grafana Cloud:** it can't reach a private network directly. Install Grafana's **private datasource connect (PDC)** agent next to this server, or run your own Grafana instead.

---

## 1. Add the datasource

### In the UI

Connections → Data sources → **Add data source** → PostgreSQL:

| Field | Value |
|---|---|
| Name | Grout |
| Host URL | `127.0.0.1:5433`, `grout-db:5432` or `<VPN IP>:5433` (see above) |
| Database name | `habits` |
| Username | `grafana_ro` |
| Password | `GRAFANA_DB_PASSWORD` from `.env` |
| TLS/SSL Mode | `disable` on localhost, a Docker network or a VPN |
| Version | 17 |
| Min time interval | `1d` |

Save & test should say "Database Connection OK".

The UI gives the datasource a random uid. Either change the dashboard to match (below), or create the datasource through the API with the uid set:

```sh
curl -u admin -H 'Content-Type: application/json' http://localhost:3000/api/datasources -d '{
  "name": "Grout", "uid": "habits-pg", "type": "grafana-postgresql-datasource", "access": "proxy",
  "url": "127.0.0.1:5433", "user": "grafana_ro",
  "secureJsonData": {"password": "<GRAFANA_DB_PASSWORD>"},
  "jsonData": {"database": "habits", "sslmode": "disable", "postgresVersion": 1700, "timeInterval": "1d"}}'
```

### By provisioning

If you run Grafana with provisioning files:
- copy `grafana/provisioning/datasources/habits.yml` into Grafana's `provisioning/datasources/`, and set `HABITS_GRAFANA_DB_PASSWORD` in Grafana's environment,
- copy `grafana/provisioning/dashboards/habits.yml` into `provisioning/dashboards/`, and `grafana/dashboards/habits.json` into the folder that file points to.

Adjust the `url:` in the datasource file to match where Grafana runs (see above), then restart Grafana.

## 2. Import the dashboard

Dashboards → New → **Import** → upload `grafana/dashboards/habits.json` → Import.

## Using a different datasource uid

Open the datasource's page; the uid is the last part of the URL (`/connections/datasources/edit/<uid>`). Then:

```sh
sed 's/"habits-pg"/"<your uid>"/g' grafana/dashboards/habits.json > /tmp/habits.json
```

and import `/tmp/habits.json` instead.

## Making your own panels

These views are worth querying. They all respect deleted rows and your timezone.

| View | One row per |
|---|---|
| `v_daily` | day: levels, hits, screen time (raw and minus driving), workouts, pages |
| `v_streaks` | habit (`move`, `read`, `phone`, `all`): current and best streak |
| `v_weekly` | ISO week: days at the minimum per habit, workouts, minutes, km, pages, average screen time |
| `v_books` | book: pages read, percent, start page |
| `v_runs` | run of consecutive days per habit |

Example, the share of days at the minimum per month:

```sql
SELECT date_trunc('month', day) AS time,
       avg(move_hit::int) * 100 AS "Move %", avg(read_hit::int) * 100 AS "Read %"
FROM v_daily WHERE tracking AND $__timeFilter(day::timestamptz) GROUP BY 1 ORDER BY 1
```

## Troubleshooting

- **"No data" on every panel:** the dashboard's datasource uid doesn't match yours (see above), or the database is empty. Log something first.
- **"pq: password authentication failed for user grafana_ro":** the password in Grafana must be `GRAFANA_DB_PASSWORD` as it was **when the database was first created**. Changing `.env` later doesn't change it. To change it:
  ```sh
  docker compose exec db psql -U habits -c "ALTER ROLE grafana_ro PASSWORD 'new'"
  ```
- **"dial tcp 127.0.0.1:5433: connect: connection refused" from a Docker Grafana:** see option B above.
- **Streaks are off by a day:** set `TZ` in `.env` to your timezone, then run `docker compose up -d`.
