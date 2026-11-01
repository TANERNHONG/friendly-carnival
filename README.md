# CARNIVAL // auction intelligence

A local eBay auction console. You give it a price list (keyword → the most you'd pay, landed in SGD). It does five things:

1. **Scans** live auctions for every keyword every 3 hours (official eBay Browse API).
2. **Tallies** each one against your price list in landed SGD: bid + shipping to SG + 9% GST, converted at live FX.
3. **Signals** deals to a **Telegram bot**, and alerts you at 60 / 15 / 5 minutes before tracked auctions end.
4. **Tracks** any listing you paste: a snapshot every 5 minutes until it sells, an extra look 90 s before the end, and the final price captured after it.
5. **Learns** from every tracked auction to predict final prices. It also builds a playbook per seller and per item: how much of the price arrives in the last 10 minutes, which end windows run cheap, and which sellers close above or below the typical price.

## Run it

```bash
pip install -r requirements.txt
python -m app
```

Open http://127.0.0.1:8787.

With no eBay keys, the app runs in **demo mode**: a simulator stands in for eBay so every screen has data. Everything it shows is labelled *Demo data*.

## Going live

1. Create an eBay developer account at https://developer.ebay.com and get a **Production** keyset (App ID = client id, Cert ID = client secret).
2. `copy .env.example .env` and fill in `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET`.
3. For Telegram, message **@BotFather**, send `/newbot`, and paste the token into `TELEGRAM_BOT_TOKEN`.
4. Delete `data/carnival.db` so the demo data goes away, then restart.
5. In Telegram, send `/start` to your bot. The first chat to do this becomes the only chat the bot talks to.

### Telegram commands

| Command | What it does |
| --- | --- |
| `/deals` | Live listings under your price list |
| `/ending` | Tracked auctions ending within 24h |
| `/list` | Everything being tracked |
| `/track <link>` | Start tracking a listing |
| `/untrack <id>` | Stop tracking |
| `/scan` | Run the keyword scan now |
| `/pricelist`, `/add <kw> = <sgd>`, `/remove <id>` | Manage the price list |

Deal messages have **Track** / **Ignore** buttons. Ending alerts include the predicted final price, the max bid that still lands on your target, and the odds of closing under it.

## How the verdicts work

- **Deal**: the model gives at least a 60% chance that the final landed cost stays at or under your target.
- **Watch**: the listing is under target now but more likely than not to finish above it.
- **Over**: the listing is already above target.

Before any auctions have completed, a listing counts as a Deal when it is under target with less than 24 hours left. The scanner also auto-tracks a few under-target listings so the model gets data. Deals that end within `AUTO_TRACK_HOURS` (default 24) are tracked automatically.

## The model

Training data is every tracked auction that ran to the end. Each one is sampled at fixed times-left (1 min … 7 days), and the model predicts the final price from the price at that moment.

- **Curve model** (from 4 sold auctions): the empirical ratio `final / price-at-T` for the same time left. It uses the most specific scope with enough history: same seller → same item → everything.
- **Quantile gradient boosting** (from 25 sold auctions). P10/P50/P90 predictions from these features:
  - time left
  - bids, plus bids and price change in the last hour
  - the seller's typical late-bid share
  - current price vs the item's typical final
  - the hour the auction ends

Both are scored on auctions they never saw: leave-one-out for the curve model, grouped k-fold for boosting. The one with the lower error is used. The 80% range is widened until it actually covers about 80% of held-out finals. The Playbook tab shows the error figures.

The model falls back to the **item's sold-price distribution** in two cases:
- the listing is further from its end than the tracked history covers (fewer than 10 sold auctions observed at that time-left)
- the price-path model's median lands below the item's 10th-percentile sale, which happens with a 99c start several days out

The UI and the Telegram alerts show which model produced each number.

## Limits

- eBay's Browse API doesn't expose bid history. The 5-minute snapshots are the history, so very fast sniping between snapshots appears as one jump.
- Ended listings can disappear from the API. If the post-end lookup fails, the last observed price is recorded as the final price.
- The default Browse API quota is 5,000 calls a day. Tracking polls in batches of 20 items, and `EBAY_DAILY_CALL_BUDGET` stops calls before the quota runs out.
- The default marketplace is `EBAY_US`, with shipping estimated to Singapore. Set `EBAY_MARKETPLACE` (or a per-row marketplace) for other sites.

## Layout

```
app/
  main.py          FastAPI app, scheduler (scan 3h, track 5m, end-watch 20s)
  scanner.py       keyword scan + price-list tally + deal signals
  tracker.py       per-listing snapshots, ending alerts, final capture
  ml.py            prediction models + playbook analytics
  telegram_bot.py  bot commands, buttons, message formatting
  ebay.py          Browse API client (OAuth client-credentials)
  demo.py          deterministic auction simulator for demo mode
  fx.py            SGD exchange rates
  db.py            SQLite storage (data/carnival.db)
web/               dashboard (vanilla JS + hand-rolled SVG charts)
```
