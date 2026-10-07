# Terrarium website

`index.html` is the living view of the organism: a glass terrarium where each
glowing honeycomb hive is an agent and the heart in the middle is the
orchestrator. Bees carry each new report to the heart; ants carry finished work
along the ground. Tap a hive to see its status, current task, P&L, the reports
it has sent while the page is open, and its finished work.

## Voice

- **Mic**: browser speech recognition (Chrome, Safari, Edge). No paid voice
  service. The private claude.ai preview may block the microphone; opened from
  your own computer in Chrome it works.
- **Answers are read aloud** with the browser's built-in voice (speaker button
  toggles it).
- **Daily briefing**: built from dashboard data (hives active, P&L, latest
  report, finished work, dark hives). The button glows until you've heard
  today's briefing; browsers only play sound after a tap.
- **Free-form questions** are answered by Claude from the dashboard data when
  the page runs on claude.ai and you allow it; elsewhere the page answers simple
  questions on its own.

Single file, no build step, no dependencies (fonts load from Google Fonts).

## Data source

The switch is at the top of the script in `index.html`:

```js
const TERRARIUM_CONFIG = { source: "sample", apiBase: "http://127.0.0.1:8787", pollMs: 5000 };
```

- `sample` runs a simulated trading session in the exact shape of
  `GET /api/dashboard`: `{agents: [{name, status, current_task, tasks_today, pnl, last_updated}], outbox: [{agent, title, detail, ts}], heartbeat: [{minute, events}]}`.
  The page labels it as sample data.
- `live` polls `GET {apiBase}/api/dashboard` (the server already sends CORS headers).

To watch the real organism on your own computer:

```bash
python -m dashboard.server          # from organism/
open "website/index.html?api=http://127.0.0.1:8787"
```

or use the Sample / Live toggle in the page header.

## Chambers

Hives come from the agents the API returns (which come from
`agents/manifest.json`). Planned agents (orchestrator, content, social, etsy,
gigs) show as dark, not-connected hives until they report. Any other
registered agent gets its own hive automatically.

Status colors follow the trader's own states: watching, in_position, idle,
sleeping, halted.

Not deployed anywhere. Making it public needs the dashboard API exposed
somewhere reachable, which is Nathan's call.

## Signal ledger

The ledger lists every signal the Terrarium sends and scores each one as if it
were taken at its signal price with $1,000, with no one-trade-at-a-time limit.
Bots that take one trade at a time can compare their own fills against it.
Exits follow the strategy: −1% stop; once a signal is up 2%, the stop trails 1%
behind the best price.

The page reads `signals` from `GET /api/dashboard`, or `GET /api/signals` if
the dashboard response has no `signals` key. **The dashboard does not serve
signals yet**; the Trader side needs to publish them. Shape, newest first:

```json
[{
  "id": "s1791340000NVDA",
  "agent": "trader",
  "ts": "2026-10-07T14:31:00+00:00",
  "symbol": "NVDA",
  "side": "buy",               // "buy" or "short"
  "price": 181.81,             // signal price (the bid the bot is told to use)
  "rsi": 14.9,
  "rel_volume": 3.3,
  "status": "open",            // "open" or "closed"
  "last_price": 182.10,        // latest price while open
  "best_pct": 0.16,            // best gain so far, % (drives the trailing stop)
  "pnl_pct": 0.16,             // current or final gain, %
  "exit_price": null,
  "exit_ts": null,
  "exit_reason": null          // "stop loss" or "trailing stop" once closed
}]
```

P&L per signal in dollars is `pnl_pct × 10` (a $1,000 position).

## Hosted scanner (live data without anyone's computer on)

`scanner.py` runs the Trader's strategy on Yahoo Finance for the **S&P 100**
with no broker attached and writes `dashboard.json` (same shape as
`/api/dashboard`, plus `scan`: what the latest minute checked and which
requirements each stock met). It reports only; it never places orders.

It runs free on GitHub Actions in a **public** repository:

- `.github/workflows/terrarium-scanner.yml` starts on weekday mornings and
  scans every minute until the close (two back-to-back jobs, because GitHub
  stops a job after 6 hours). After each minute `publish.sh` pushes the file
  to the `terrarium-data` branch.
- `.github/workflows/terrarium-site.yml` publishes this folder to GitHub
  Pages. On `*.github.io` the page finds
  `raw.githubusercontent.com/<owner>/<repo>/terrarium-data/dashboard.json`
  by itself and opens in Live mode.

Limits of the free route: GitHub can start scheduled jobs late, sometimes by
10 minutes or more; the raw file can be up to about 5 minutes behind; and
Yahoo may throttle GitHub's servers (the scanner backs off and keeps the last
good data).

Test without the network: `python website/scanner.py --replay DIR --out out.json`.
Point any copy of the page at a feed with `index.html?feed=<url>`.

## Console voice (Whisper + Fish Audio)

`website/voice_server.py` runs on your computer next to the console. The site
(local or the public GitHub Pages copy) checks `http://127.0.0.1:8788` when it
loads; if the voice server answers, the mic records through Whisper and answers
are read aloud by Fish Audio. If not, the site uses the browser's free voice.

```
pip install faster-whisper            # free, runs locally; first use downloads the model
set FISH_AUDIO_API_KEY=your-key       # Windows; use export on Mac/Linux. Paid per use.
set FISH_AUDIO_VOICE_ID=voice-id      # optional, any voice from fish.audio
python website/voice_server.py
```

Optional: `WHISPER_MODEL` (default `base.en`), `FISH_AUDIO_MODEL`, `VOICE_PORT`.
The key stays on your computer; it never goes into the site or the repo.
Agents can call the same functions: `from website.voice_server import transcribe, speak`.
Only pages on heyjensen.github.io, localhost, or opened as a local file can use the server.
