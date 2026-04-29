# coc-tracker

Telegram bot that tracks **cumulative donations across a multi-clan family** in Clash of Clans.

## Why this exists

The Clash of Clans API resets a player's `donations` field to `0` whenever they switch clans. For clan families that share members across several clans (e.g. "War Snipers" with 5 clans), this means built-in donation leaderboards can't fairly rank members who hop between clans during a season.

`coc-tracker` solves this with a **bonus pattern**: every time a player moves to a different clan, their previous donation total is locked in as `bonus`. The leaderboard total is always `bonus + last_donations`. A drop in the donation count without a clan change is detected as a "rejoin" (player left and rejoined the same clan, which also resets to 0) and handled the same way.

The result: jumping between any of the tracked clans does **not** reset your seasonal donation count.

## Features

- ⚡ **Background sync every 10 s** — button presses serve from in-memory cache, so leaderboards appear instantly
- 🔁 **Cross-clan cumulative totals** via the bonus pattern (move + rejoin both handled)
- 📅 **Season rollover** detected via Gold Pass API; previous season snapshotted and kept for 2 weeks
- 🏰 **Per-clan and combined leaderboards** with medal emojis and live "synced Xs ago" indicator
- 🔒 **Group-only commands** — bot only responds inside the configured group
- 🧠 **Tag discovery from group description** — drop clan tags into the Telegram group description and the bot picks them up

## Commands

| Command | Description |
|---|---|
| `/donation` | Combined leaderboard across all clans |
| `/clanlist` | Donations grouped by each clan |
| `/lastseason` | Final standings from last season (kept for 2 weeks) |
| `/checktags` | Verify which clan tags the bot is tracking |
| `/menu` | Show inline-button menu |
| `/help` | Command help |

## Quick start (Docker)

```bash
git clone https://github.com/jackjackliew/coc-tracker.git
cd coc-tracker
cp .env.example .env
# Edit .env and fill in TELEGRAM_TOKEN + COC_API_TOKEN
docker compose up -d
docker compose logs -f
```

The first run creates an empty `donation_storage.json` next to the bot. It populates within ~10 s once the group description has clan tags.

## Quick start (systemd, the way it runs in production)

```bash
git clone https://github.com/jackjackliew/coc-tracker.git /home/ubuntu/coc-tracker
cd /home/ubuntu/coc-tracker
python3.12 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env  # then edit it
sudo cp coc-tracker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now coc-tracker
journalctl -u coc-tracker -f
```

## Configuration

Set in `.env` (see `.env.example` for the full list):

| Variable | Required | Default | Notes |
|---|---|---|---|
| `TELEGRAM_TOKEN` | yes | — | from `@BotFather` |
| `COC_API_TOKEN` | yes | — | from [developer.clashofclans.com](https://developer.clashofclans.com/) — must match the bot host's IP |
| `COC_POLL_INTERVAL` | no | `10` | seconds between background syncs |
| `COC_SEASON_CHECK_INTERVAL` | no | `600` | seconds between season rollover checks |
| `COC_CLAN_NAME_REFRESH` | no | `3600` | seconds between clan-name refreshes |
| `COC_HTTP_TIMEOUT` | no | `10` | HTTP client timeout |
| `COC_LAST_SEASON_RETENTION_DAYS` | no | `14` | how long last-season snapshot is kept |
| `LOG_LEVEL` | no | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

## Setting up the Telegram group

1. Add the bot to a group / supergroup
2. Promote it to admin (so it can read the group description)
3. Put the clan tags inside the group description, e.g.:
   ```
   War Snipers Family — clans: #ABC123 #DEF456 #GHI789 #JKL012 #MNO345
   ```
4. Run `/checktags` in the group to confirm detection

## Architecture

```
bot.py                    # Thin entrypoint kept at repo root (systemd compat)
coc_tracker/
├── __init__.py
├── config.py             # Constants + env-loaded settings
├── storage.py            # DonationStorage — JSON persistence (atomic writes)
├── api.py                # ClashAPI — async httpx wrapper for the CoC REST API
├── tracker.py            # DonationTracker — orchestrator (sync/season/clan-name refresh)
├── keyboard.py           # Inline keyboard builder
├── handlers.py           # Telegram command + callback handlers
└── main.py               # Application setup + main()
tests/
├── conftest.py
└── test_storage.py       # DonationStorage edge cases (move, rejoin, season rollover)
```

### Storage schema (v1, preserved across upgrades)

```json
{
  "season_key":  "20260401",
  "clan_tags":   ["#ABC123", "#DEF456"],
  "clan_names":  {"#ABC123": "War Snipers"},
  "last_sync":   "2026-04-13T08:58:23",
  "players": {
    "#PLAYERTAG": {
      "name": "PlayerName",
      "bonus": 1000,
      "last_clan": "#ABC123",
      "last_donations": 250
    }
  }
}
```

`bonus` accumulates locked-in donations every time a player moves clans or rejoins. Total displayed = `bonus + last_donations`.

## Development

```bash
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

## Tech stack

- Python 3.10+ (3.12 in CI)
- [`python-telegram-bot[job-queue]`](https://python-telegram-bot.org/) — async Telegram framework with built-in JobQueue
- [`httpx`](https://www.python-httpx.org/) — async HTTP/2 client for the CoC API
- JSON file storage (atomic writes via tmp-and-rename)

## License

MIT — see [LICENSE](./LICENSE).
