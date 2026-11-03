# Connecting the Grout app

The app works without a server. Connecting one is optional, and you can disconnect again at any time without losing anything on the phone.

## What you need

- the app: [Grout on Google Play](https://play.google.com/store/apps/details?id=com.rainybit.grout)
- the server running and reachable over **HTTPS**, e.g. `https://habits.example.com` (see the README)
- the server's **API token**: the `API_TOKEN` value in `.env`

The app refuses `http://` addresses, and Android blocks unencrypted traffic anyway. Self-signed certificates don't work; use a real one (Caddy or Let's Encrypt).

## 1. Create a token

If `API_TOKEN` in `.env` is still empty:

```sh
openssl rand -hex 24          # e.g. 3f9c…  (48 characters)
```

Put it in `.env` as `API_TOKEN=3f9c…`, then apply it:

```sh
docker compose up -d
```

Check it works before touching the phone:

```sh
curl -H "Authorization: Bearer 3f9c…" https://habits.example.com/stats     # JSON, not 401
```

The token gives full read and write access to your habit data. Treat it like a password: don't post it in chats or commit it.

## 2. Get the token onto the phone

Avoid sending it through chat apps or email. Easy, safe ways:

- **Password manager:** save it as an item (e.g. "Grout server token") and copy it from the password manager app on the phone.
- **Type it:** 48 characters, tedious but fine.
- **QR code on your own screen:** `qrencode -t ansiutf8 "3f9c…"` in a terminal, then scan it with the phone camera and copy the text.

## 3. Connect

In Grout:

1. Open **Settings** (bottom right) and scroll to **Server sync (optional)**.
2. **Server address:** `https://habits.example.com`, with no path and no trailing slash needed.
3. **Token:** paste the API token.
4. Tap **Connect and sync**.

You should see **"✓ Connected and synced"**, and under the button something like *"Synced: 12 sent, 40 received"*.

**What happens on the first sync:**
- Everything already on the phone is uploaded, and everything on the server comes down. Nothing is deleted.
- If both sides have a check-in for the same day, they're merged field by field.
- For books, workouts and reading, the newest edit wins.
- From then on the app syncs after every change, every time you open it, and about once an hour in the background. The Today screen shows *"Synced 14:35"* under the date, and a ↻ button appears for syncing by hand.

## What syncs, and what doesn't

| Syncs | Stays on the phone |
|---|---|
| check-ins (3 goals, move, read, phone out) | reminder times and on/off days |
| screen time per day (total + driving) | per-app screen time |
| workouts, reading, books | widget and app settings |
| targets (screen time, weekly goals) | the token itself (also excluded from Google backup) |

Reminders always come from the phone, so they work offline. If you also use the Telegram bot, set `NOTIFY_TELEGRAM=false` on the server so the bot doesn't send them too.

## Several phones

Connect each phone with the same address and token, and they all share one set of data. The server holds **one person's** habits; it isn't multi-user. For a second person, run a second copy of the server, with its own `.env`, port and domain.

## Changing the token

If it leaked, or just from time to time:

1. Put a new value in `.env` (`openssl rand -hex 24`) and run `docker compose up -d`. The old token stops working immediately.
2. In the app: Settings → Server sync → paste the new token → **Connect and sync**.

Until you do step 2, the app keeps working offline and shows *"Not synced: The server rejected the token"*. Nothing is lost; it catches up once the new token is in.

## Disconnecting

Settings → Server sync → **Disconnect**. The app forgets the address and token and keeps all data on the phone. The server keeps its copy too. Reconnecting later syncs both ways again.

## Moving to a new phone

- **Google backup** (same Google account): everything comes back, including reminder times and the server connection. Only the token is left out on purpose: enter it again under Server sync.
- **Server only:** install Grout, connect it as above, and the history comes down from the server. Set your reminder times again: they're stored per phone, not on the server.

## Troubleshooting

| The app says | Fix |
|---|---|
| "Use an https:// address" | Enter the address with `https://` |
| "No connection to the server" | Check the domain resolves and the proxy runs: `curl https://habits.example.com/health` from another network, e.g. phone data |
| "The server rejected the token" | The token doesn't match `API_TOKEN`. Look for a stray space when pasting, or did you change it and not run `docker compose up -d`? |
| "The server had a problem" | `docker compose logs app` on the server |
| "Not synced …" under the date | The last sync failed. Data is safe on the phone; it retries when you open the app and every hour |
| Old data missing after connecting | Sync only brings what the server has. Check `curl -H "Authorization: Bearer …" https://…/dashboard` |
| A certificate error | Self-signed or expired certificate. Use Caddy or Let's Encrypt |
