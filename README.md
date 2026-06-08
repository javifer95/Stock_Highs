# Stock Outperformance Alerts

A free, zero-server tool that scans the whole US stock market once a day after
the close and pushes a notification to your iPhone when a stock is *far*
outperforming the market. No app to build, no Apple Developer account, no
recurring cost.

## How it works

```
GitHub Actions (daily cron)
   -> scan.py
        -> fetch US common-stock list (NASDAQ/NYSE symbol files)
        -> download ~3 months of daily prices (yfinance)
        -> compute today's return in excess of SPY, as a z-score
        -> keep liquid names with volume-confirmed moves
        -> POST survivors to ntfy
                -> push notification on your phone
```

## The signal

For each stock: `excess = stock_daily_return - SPY_daily_return`. Today's excess
return is converted to a **z-score** against the stock's own prior 60 trading
days of excess returns. A hit requires **all** of:

- z-score >= **3.0** (today's move is 3+ std devs beyond the stock's normal)
- excess return is positive (upside only)
- price >= **$5** (no penny stocks)
- average daily dollar volume >= **$5,000,000** (liquid enough to matter)
- today's volume >= **1.5x** its recent median (the move is real, not a fluke)

All of these live in the CONFIG block at the top of `scan.py` — tune freely.
Raise `Z_THRESHOLD` for fewer/stronger alerts; lower it for more.

## One-time setup (about 10 minutes)

1. **Install the ntfy app** on your iPhone (App Store, free). Open it, tap "+",
   and subscribe to a topic with a long random name you choose, e.g.
   `stock-alerts-8fk39dlz`. Anyone who knows the name can read the topic, so
   keep it unguessable (or self-host ntfy later if you care).

2. **Create a GitHub repo** and add these files. A **public** repo gives you
   unlimited free Actions minutes; a private one gets 2,000 free min/month,
   which is also plenty (each run takes a few minutes).

3. **Add the topic as a secret:** repo Settings -> Secrets and variables ->
   Actions -> New repository secret. Name it `NTFY_TOPIC`, value is your topic
   name from step 1. (Optional `NTFY_SERVER` secret only if you self-host.)

4. **Enable Actions** (Actions tab -> enable workflows). Then click into
   "Daily outperformance scan" -> **Run workflow** to test it immediately
   instead of waiting for the schedule.

That's it. It now runs itself every weekday after the close.

## Testing locally

```bash
pip install -r requirements.txt
NTFY_TOPIC=your-topic-name python scan.py     # real push
python scan.py                                # no topic -> prints to console
python test_signal.py                         # unit-test the signal math
```

## Known limitations / honest caveats

- **yfinance is unofficial** (it scrapes Yahoo) and breaks occasionally. If a
  run fails, it's usually this. Stooq bulk CSV is a free fallback data source.
- **EOD only.** This tells you what already happened today, after the close —
  it is not an intraday or trading signal.
- **Symbol mapping is imperfect.** Some class shares / special tickers may not
  resolve in yfinance and get silently skipped.
- **"Outperforming" = attention, not opportunity.** A name that's already
  spiked is often already priced in. Decide what you'll actually do with these.
- GitHub may **disable scheduled workflows** on a repo with no activity for 60
  days; just push a commit or click Run workflow to keep it alive.
