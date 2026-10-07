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
