# Stock Outperformance Alerts

A free, zero-server tool that scans the US stock market once a day after the
close and pushes a notification to your iPhone when a stock is *far*
outperforming the market. No app to build, no Apple Developer account, no cost.

## How it works

```
GitHub Actions (daily cron)
   -> scan.py
        -> fetch US common-stock list (NASDAQ/NYSE symbol files)
        -> fetch SPY first (benchmark), then refresh prices via yfinance
           using a CACHE so we never hammer Yahoo with thousands of requests
        -> compute each stock's return in excess of SPY, as a z-score
        -> keep liquid names with volume-confirmed moves
        -> POST survivors to ntfy  -> push notification on your phone
```

### Why the cache (important)

Yahoo rate-limits thousands of rapid requests from a cloud IP, so the scanner
cannot pull all ~7,000 tickers at once. Instead it keeps a small price-history
cache that GitHub's Actions cache carries between runs:

- **SPY is always fetched first**, on its own, with retries -- the run never
  dies for lack of a benchmark.
- The **first few daily runs backfill** the whole market a couple thousand
  names at a time. Expect partial coverage (and possibly no alerts) for the
  first ~3-4 runs while the cache fills.
- After that, each run only refreshes the **liquid** names that could actually
  trigger an alert, which stays under Yahoo's limit indefinitely.
- Partial coverage never crashes the run; it scans whatever is fresh and prints
  the coverage %. You'll see this in the run log.

## The signal

`excess = stock_daily_return - SPY_daily_return`, converted to a z-score against
the stock's own prior 60 trading days. A hit requires ALL of:

- z-score >= **3.0**, and the move is to the upside
- price >= **$5** and average daily dollar volume >= **$5,000,000**
- today's volume >= **1.5x** its recent median

All tunable in the CONFIG block of `scan.py`. Raise `Z_THRESHOLD` for fewer,
stronger alerts.

## One-time setup

1. **Install the ntfy app** (iPhone, free) and subscribe to a long random topic
   name, e.g. `stock-alerts-8fk39dlz`.
2. Put these files in a **GitHub repo** (public = unlimited free Actions).
3. Add the topic as a secret: Settings -> Secrets and variables -> Actions ->
   New repository secret, named `NTFY_TOPIC`.
4. Actions tab -> enable workflows -> **Run workflow** to test.

Run it a few days in a row (or click Run workflow a few times) to let the cache
warm up to full coverage.

## Testing locally

```bash
pip install -r requirements.txt
NTFY_TOPIC=your-topic python scan.py     # real push
python scan.py                           # no topic -> prints to console
python test_signal.py                    # signal math
python test_cache.py                     # cache + end-to-end (mocked network)
```

## Honest caveats

- **yfinance is unofficial** and rate-limits hard from cloud IPs; the cache is
  the workaround. Coverage builds over the first few runs.
- **EOD only** -- this reports what already happened; it is not a trading signal.
- **"Outperforming" = attention, not opportunity.** A name that already spiked
  is often already priced in.
- GitHub disables scheduled workflows after 60 days of repo inactivity; a commit
  or manual run resets that.
