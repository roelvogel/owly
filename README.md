# Owly

### Your topics. No feed. No phone.

Owly is a personal news digest for people who want to stay sharp on **tech**, **stocks**, or a niche they actually care about — without opening social media or living on their phone.

You name the beat. Owly gathers it from RSS and live X, Grok keeps only the signal, and you get a clean Markdown edition — twice a day, then you’re done. Read it on an e-ink tablet. Close the app. Get on with your life.

**[github.com/roelvogel/owly](https://github.com/roelvogel/owly)** · **Tablet app:** [Owly Android](https://github.com/roelvogel/owly-android)

---

## Why Owly

Following markets, semiconductors, or a research niche usually means the same trap: Twitter, Bloomberg tabs, push alerts, and an hour you didn’t mean to spend.

Owly flips that.

| The usual way | With Owly |
|---------------|-----------|
| Infinite feeds & “for you” noise | Only the topics *you* configure |
| Phone in hand all day | Optional tablet / e-ink reading |
| Headlines fighting for attention | Short, curated Markdown digests |
| Always-on notifications | Morning + evening. Then silence |

Built for people who want **specificity**, not another timeline.

## Who it’s for

- Tracking a handful of tickers without refreshing X all day  
- Following AI, chips, or an industry niche without doomscrolling  
- Reading news on a Boox (or similar) instead of a phone  
- Anyone who wants to stay informed — and stay out of the feed  

## What you get

| Edition | What it covers |
|---------|----------------|
| **Main digest** | Top stories from your RSS feeds + X topics, curated by Grok |
| **Stocks** *(optional)* | Per-ticker signal from X — configured locally, never shipped in the repo |

Under the hood: full-text RSS (not just headlines), live X search via Grok’s `x_search`, SQLite dedupe so repeats don’t sneak back in, and a local dashboard to manage sources and trigger runs.

## How it works

```
Your feeds & topics  →  Grok curation  →  Markdown digest
                              ↓
                     Read on e-ink / tablet
```

Schedule it (e.g. 07:00 and 21:00) or hit **Run** when you want. Pair with the [Android client](https://github.com/roelvogel/owly-android) over Tailscale — trigger and read from your tablet, still without touching social apps.

## Quick start

**Requirements:** Python 3.11+ and an [xAI API key](https://console.x.ai).

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
# Edit .env → set XAI_API_KEY
```

```powershell
python -m owly.run                  # generate a digest
python -m owly.run --edition morning
python -m owly.dashboard            # open http://localhost:8741
.\scripts\register_tasks.ps1        # schedule 07:00 / 21:00 on Windows
```

Add RSS feeds, topics, and (optionally) stock tickers in the dashboard. Your watchlist and digests stay on your machine.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `XAI_API_KEY` | — | xAI API key |
| `XAI_MODEL` | `grok-4.5` | Model name |
| `OUTPUT_DIR` | `editions` | Where digests are written |
| `INGESTION_HOURS` | `12` | Lookback for RSS and X |
| `DASHBOARD_PORT` | `8741` | Dashboard / API port |
| `OWLY_API_KEY` | — | Secret for `/api/*` (optional) |

## Read on a tablet

The companion app for Boox / Android:

**[github.com/roelvogel/owly-android](https://github.com/roelvogel/owly-android)**

Trigger runs, list editions, read Markdown — over Tailscale to your home PC. Built for a ~10" e-ink layout: big type, high contrast, no clutter.

## JSON API

Set `OWLY_API_KEY` in `.env`. Routes require `X-Api-Key` (or `Authorization: Bearer …`).

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/status` | Run status + latest run |
| `POST` | `/api/run` | Start a run |
| `GET` | `/api/editions` | List digests |
| `GET` | `/api/editions/{filename}` | Raw Markdown |

```powershell
curl -H "X-Api-Key: your-secret" http://localhost:8741/api/status
```

## Private by design

Gitignored and never published:

- `.env` — keys and secrets  
- `data/` — your feeds, topics, **stock tickers**, history  
- `editions/` — generated digests  

No default stock watchlist. Your portfolio and niches stay yours.

## Project layout

```
owly/
  config.py · db.py · ingest.py · grok.py · render.py
  run.py · dashboard.py · api.py
  templates/ · static/
scripts/register_tasks.ps1
editions/   # gitignored
data/       # gitignored
```

## License

MIT — see [LICENSE](LICENSE).

---

*Specific topics. Zero distraction. Phone optional.*
