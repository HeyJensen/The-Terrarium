# Phase 2: the commerce bots

Written 2026-10-07. Four bots that research, make, write and promote, built in
**draft-only** mode: they run end to end today on files you give them, and
everything they make waits in an approval queue. Nothing is listed, posted,
published or bought.

## How they connect

```
 trend files ──► SOCIAL scan ──► [trends board] ──┬──► ETSY research ◄── keyword exports (eRank etc.)
                                                  │         │
                                                  │         ▼
                                                  │   [niches board]
                                                  │      │       │
                                                  ▼      ▼       ▼
                                             CONTENT (blog)   STUDIO (t-shirts, printables)
                                                  │                │
                                                  ▼                ▼
                                        ┌────── approval queue (you) ──────┐
                                        │  approve / reject each draft     │
                                        └──────────────┬───────────────────┘
                                                       ▼
                                          SOCIAL promote: posts for each approved
                                          listing / blog post ──► approval queue
```

| Bot | Folder | Reads | Writes |
|---|---|---|---|
| Social | `agents/social` | trend files in `state/inbox/social/`; approved items | `trends` board; social post drafts |
| Etsy research | `agents/etsy` | keyword exports in `state/inbox/etsy/`; `trends` board | `niches` board; `state/etsy/keywords_to_check.txt` |
| Studio | `agents/studio` | `niches` board | t-shirt and printable listing drafts |
| Content | `agents/content` | `niches` + `trends` boards | blog post drafts |

Shared pieces live in `agents/common/`: the board (`board.py`), the approval
queue (`approvals.py`), the draft-only lock and IP check (`bot.py`,
`ip_blocklist.json`), and the writing engine (`brain.py`).

## Try it

```bash
mkdir -p state/inbox/social state/inbox/etsy
cp agents/common/samples/trends.csv state/inbox/social/
cp agents/common/samples/keywords_export.csv state/inbox/etsy/

python agents/social/run.py --task scan       # 4 trends to the board
python agents/etsy/run.py --task scan         # 4 niches to the board
python agents/studio/run.py --task draft      # 6 product drafts
python agents/content/run.py --task draft     # 3 blog drafts

python -m agents.common.approvals list
python -m agents.common.approvals show <id>
python -m agents.common.approvals approve <id>
python agents/social/run.py --task promote    # promo posts for what you approved
```

## Things that shaped the design

- **The Etsy bot can't scan Etsy.** Etsy's API terms ban scraping the site
  and using Etsy data "for purposes of analytics" without written permission.
  So research numbers come from a tool you subscribe to (eRank, EverBee,
  Marmalead) as an export you drop in the inbox. Trends it has no numbers for
  go on a to-check list for your next export.
- **"Similar products" means same niche, original design.** Since August 11,
  2026 Etsy requires designs to be the seller's own, AI use to be disclosed,
  and production partners to be named. The studio works only from keywords,
  writes both disclosures into every listing, and flags brand, character,
  team and celebrity names. (Our sample "taylor swift" trend gets no product
  for this reason.)
- **Blog drafts are skeletons for you to edit.** Google demotes mass-produced
  AI pages and AdSense rejects thin sites. The Amazon disclosure is always the
  first line, and product links are placeholders: Amazon's product API
  (Creators API) only opens after 10 sales in 30 days.
- **The template brain writes placeholder copy.** It's free and makes the
  pipeline testable. Real copy needs the Claude brain (decision 2 below).

## Decisions for you

Nothing below is set up. Each needs your OK because it costs money, needs an
account, or adds a dependency. Prices were checked 2026-10-07 unless marked
as an estimate.

| # | What | Needed for | Cost | Notes |
|---|---|---|---|---|
| 1 | **Etsy research tool** (eRank recommended) | Etsy research | eRank $5.99–29.99/mo; EverBee $29.99–99/mo | You export keyword lists; the bot never logs in. |
| 2 | **Anthropic API key + `anthropic` package** | Real writing in studio, content, social | Sonnet 5.5: $2 / $10 per million tokens in/out. Estimate: about $0.02–0.05 per blog draft | Without it the copy stays template placeholders. |
| 3 | **Image generation** for t-shirt art | Studio | Paid image API, estimate a few cents per image; or you design in Canva from the bot's prompts | No image tool is chosen yet. |
| 4 | **Etsy shop + Etsy API app** | Listing approved drafts | $0.20 per listing, 6.5% transaction fee, about 3% + $0.25 processing | API app needs Etsy's approval; only used for our own shop. |
| 5 | **Printify or Printful** | Printing and shipping shirts | Free plans; you pay base cost per shirt when it sells | Printify Premium ($29/mo) only lowers base costs at volume. |
| 6 | **Blog site + domain** | Content | Estimate: domain ~$10–20/yr; hosting free (static) to ~$15/mo (WordPress) | Needs a publisher module once chosen. |
| 7 | **Google AdSense** | Blog revenue | Free | Approval needs a site with real, original posts first. |
| 8 | **Amazon Associates** | Blog revenue | Free | Account closes without 3 qualifying sales in its first 180 days, so don't apply until the blog has traffic. |
| 9 | **Social accounts and APIs** | Social posting | Pinterest, Instagram/Facebook, TikTok APIs: free but each needs app review (TikTok posts stay private until audited). X: $0.015 per post, $0.20 with a link | Pinterest first: it drives Etsy and blog traffic best for the least setup. |
| 10 | **Live trend sources** | Social scan | Google Trends (official API in limited access, or the unofficial `pytrends` package), Pinterest Trends, Reddit (commercial use needs approval) | Until then, trend files go in the inbox. |

Skool has no public posting API, so the social bot can draft Skool posts
for you to paste, but can't post there.

**Suggested order:** 1 and 5 first (about $6/mo) so the Etsy loop works with
you approving each design, then 2 to make the copy real, then 6–8 for the
blog, then 9 starting with Pinterest. Each publisher gets built only after
you approve its account, and every publisher reads only approved items.

## Proposed for shared/ (owned by the Trader thread)

- Move `agents/common/board.py` and `approvals.py` into `shared/` once the
  trader wants them (its live-entry `y` prompt could use the same queue).
- Dashboard: an `/api/approvals` endpoint with pending counts per agent, so
  the website and dashboard show what's waiting on you.
