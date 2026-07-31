# Owly

**AI-powered news digest for e-ink readers.**

**Repository:** [github.com/roelvogel/owly](https://github.com/roelvogel/owly)

Owly is a personal news curation service that turns RSS feeds and real-time X (Twitter) into clean Markdown digests — designed for e-ink tablets like the Boox Note Air. It uses Grok (`x_search`) to find signal, skip noise, and write short, readable summaries twice a day.

Pair it with the [Owly Android client](https://github.com/roelvogel/owly-android) to trigger runs and read editions over Tailscale from your tablet.

## What it does

- **AI news digest** — Grok picks the highest-signal stories from your feeds and topics
- **Full-text RSS** — downloads article bodies (not just headlines) with `trafilatura`
- **Live X search** — pulls recent posts via xAI’s native `x_search` tool
- **E-ink Markdown** — large headings, short paragraphs, no tables or clutter
- **Optional stocks edition** — ticker digests from X (configured locally; not in the repo)
- **Local dashboard** — manage sources, browse editions, run on demand
- **JSON API** — authenticated endpoints for remote clients (VPN / Tailscale)

## How it works

```
RSS feeds + X topics  →  Grok curation  →  Markdown editions
                              ↓
                     SQLite (dedupe / history)
```

Runs on a schedule (e.g. 07:00 and 21:00) or whenever you hit **Run** in the dashboard or Android app. Deduplication means you don’t see the same URL twice.

## Requirements

- Python 3.11+ recommended (3.9+ may work)
- An [xAI API key](https://console.x.ai)

## Quick start

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
# Edit .env → set XAI_API_KEY
```

Generate a digest:

```powershell
python -m owly.run
python -m owly.run --edition morning
python -m owly.run --edition evening
python -m owly.run --dry-run   # RSS only, no API calls
```

Start the dashboard:

```powershell
python -m owly.dashboard
```

Open `http://localhost:8741`.

Schedule twice-daily runs on Windows:

```powershell
.\scripts\register_tasks.ps1
```

## Configuration

Copy `.env.example` to `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `XAI_API_KEY` | — | xAI API key |
| `XAI_MODEL` | `grok-4.5` | Model name |
| `OUTPUT_DIR` | `editions` | Where Markdown digests are written |
| `INGESTION_HOURS` | `12` | Lookback window for RSS and X |
| `DASHBOARD_PORT` | `8741` | Dashboard / API port |
| `OWLY_API_KEY` | — | Shared secret for `/api/*` (optional) |

## Android client

The companion app for Boox / Android tablets lives in a separate repo:

**[github.com/roelvogel/owly-android](https://github.com/roelvogel/owly-android)**

It talks to this server’s JSON API over Tailscale: trigger morning/evening runs, list editions, and read Markdown on device.

## JSON API

Set `OWLY_API_KEY` in `.env`. All `/api/*` routes require `X-Api-Key` (or `Authorization: Bearer …`).

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/status` | Run in progress + latest run |
| `POST` | `/api/run` | Start a run (`{"edition": "morning" \| "evening" \| null}`) |
| `GET` | `/api/editions` | List digests (newest first) |
| `GET` | `/api/editions/{filename}` | Raw Markdown for one edition |

```powershell
curl -H "X-Api-Key: your-secret" http://localhost:8741/api/status
curl -H "X-Api-Key: your-secret" http://localhost:8741/api/editions
curl -X POST -H "X-Api-Key: your-secret" -H "Content-Type: application/json" `
  -d "{\"edition\":\"morning\"}" http://localhost:8741/api/run
```

## Private data (not in this repo)

These stay on your machine and are gitignored:

- `.env` — API keys and secrets
- `data/` — SQLite (feeds, topics, **stock tickers**, run history)
- `editions/` — generated Markdown digests (including stocks)

Add stock tickers yourself in the dashboard. No default watchlist ships with the project.

## Project layout

```
owly/
  config.py      # Settings from .env
  db.py          # SQLite persistence
  ingest.py      # RSS + full-text extraction
  grok.py        # xAI / Grok client + x_search
  render.py      # E-ink Markdown renderer
  run.py         # CLI entrypoint
  dashboard.py   # FastAPI dashboard
  api.py         # JSON API for remote clients
  templates/     # Dashboard HTML
  static/        # E-ink CSS
scripts/
  register_tasks.ps1
editions/        # Generated digests (gitignored)
data/            # SQLite database (gitignored)
```

## E-ink formatting

Digests use strict Markdown: `#` title, `##` per item (or `##` ticker / `###` item for stocks), short paragraphs, no tables or emojis. Source URLs are tracked internally but not printed in the edition.

## License

MIT — see [LICENSE](LICENSE).
