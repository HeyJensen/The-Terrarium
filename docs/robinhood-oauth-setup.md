# Robinhood Agentic Trading: connection setup

Researched 2026-10-07. Robinhood changes this product often (it is in beta),
so re-check the official pages before each step.

## What Robinhood actually offers (verified)

There is **no stock-trading REST API with API keys**. Robinhood's official
route for software agents is **Agentic Trading**, launched in beta on
May 27, 2026:

- It is an **MCP server** (Model Context Protocol) at
  `https://agent.robinhood.com/mcp/trading`, connected over HTTP.
- Authentication is **OAuth in the browser**: your agent client opens a
  Robinhood login page, you approve, and the client holds the token. You never
  give the agent your password, and there is no API key to put in `.env`.
- Trades go **only to a dedicated "MCP" (Agentic) account**, a separate
  self-directed individual account you open and fund. The agent can *read* your
  other Robinhood accounts but can only trade in that one.
- **Equities only** for now; options, crypto, futures are "coming soon".
- You can watch activity, get a notification on every trade, and disconnect the
  agent instantly from the Robinhood app.

Sources: [Robinhood support: Onboarding an external agent](https://robinhood.com/us/en/support/articles/agentic-trading-overview/),
[Robinhood newsroom: Robinhood is now open to agents](https://robinhood.com/us/en/newsroom/robinhood-is-now-open-to-agents/).

## Setup steps for Nathan

1. **Primary account in good standing.** Agentic accounts are only offered to
   customers who already have an individual investing account. Use the
   project email (`organism@…`) only if Robinhood lets you open a new login
   with it; otherwise the agentic account sits under your existing login and
   the project email is just for alerts.
2. **Watch for the access email / check the app.** Access is rolling out in
   stages; Robinhood emails eligible customers.
3. **Open the Agentic (MCP) account** in the Robinhood app (desktop web is
   what the guides use). Fund it with exactly the $1,000 risk capital.
4. **Connect an MCP client and complete OAuth in your browser.** For Claude
   Code this is:
   ```
   claude mcp add --transport http robinhood-trading https://agent.robinhood.com/mcp/trading
   ```
   Then trigger any Robinhood tool; a browser window opens for Robinhood login
   and approval. A third-party review reports the OAuth redirect only
   completes on `localhost`, so do this on your own computer, not a server.
5. **Turn on trade notifications** in the Robinhood app and learn where the
   **disconnect agent** button is before any live order.
6. **Tell Claude the result** (account type shown as cash or margin, and
   whether short selling is offered on it). Do not paste tokens into chat.

## What I could NOT confirm

| Question | Status |
|---|---|
| Can the Agentic account be a **margin** account / allow **short selling**? | Not in Robinhood's docs. One third-party setup guide calls it long-only. Treat shorting as unavailable until you see it in the app. |
| **Historical 1-minute bars** and daily volume through the MCP | Not documented. Third-party tool lists show quotes (`get_equity_quotes`) but no candles/historicals. The strategy needs both, so we likely need a separate market-data source. |
| Exact **tool names and request/response schemas** | Robinhood publishes none. Third parties list `get_accounts`, `get_portfolio`, `get_equity_positions`, `get_equity_quotes`, `place_equity_order`, `cancel_equity_order`, `get_equity_orders`. Must be checked against the live server. |
| **Order types** (stop orders resting at the broker?) | Market and limit confirmed by a third party; stops unconfirmed. The engine therefore watches stops itself and sends a market order, which only works while the engine is running. |
| **Rate limits, token lifetime, re-auth** | Not documented. |
| **Paper trading** | None offered. |
| **Calling the MCP server from our own Python code** (not from a chat client) | Possible in principle with an MCP client library, but undocumented by Robinhood and it adds a dependency. Needs your OK. |

Third-party sources (not authoritative): [coil.trade setup guide](https://coil.trade/guides/robinhood-agentic-trading-setup),
[NexusTrade review](https://nexustrade.io/blog/robinhood-agentic-trading-mcp-review-20260708),
[Vorp Labs overview](https://vorplabs.com/agent-tools/robinhood-cli),
[TechCrunch launch coverage](https://techcrunch.com/2026/05/27/robinhood-now-lets-your-ai-agents-trade-stocks/).

## How the code will use it

`agents/trader/broker.py` has a `RobinhoodMCPBroker` placeholder that refuses
every call. Wiring it needs, in order: your approval of an MCP client library,
an OAuth token cache path in `ROBINHOOD_OAUTH_TOKEN_FILE`, a schema check of the
live tools, and a market-data source for 1-minute bars. Until then the engine
can only run in dry run / replay.
