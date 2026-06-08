#!/usr/bin/env python3
"""
Whole-market end-of-day outperformance scanner (cached / rate-limit-safe).

Run once a day from GitHub Actions. It:
  1. Fetches the full list of US common stocks (NASDAQ + NYSE/other listings).
  2. Pulls daily prices via yfinance -- but NOT all at once. Yahoo rate-limits
     thousands of requests from a cloud IP, so instead we keep a small on-disk
     CACHE of price history (carried between runs by GitHub's Actions cache).
       * The benchmark (SPY) is always fetched first, on its own, with retries.
       * Each run refreshes the liquid names that could actually trigger an
         alert, plus a rotating slice of everything else to fill/refresh the
         cache. The first few runs backfill the whole market; after that each
         run only touches ~a couple thousand names -- under Yahoo's limit.
       * Partial coverage never aborts the run; we scan whatever is fresh.
  3. For each stock with TODAY's bar, computes its return in excess of SPY as a
     z-score against its own recent excess-return distribution, gated by price,
     dollar-volume, and a volume-confirmation filter.
  4. Pushes the survivors to your phone via ntfy.

No API keys. The only secret is your ntfy topic name (a GitHub Actions secret).
"""

from __future__ import annotations

import io
import os
import sys
import time
import datetime as dt
from dataclasses import dataclass

import numpy as np
import pandas as pd
import requests
import yfinance as yf

# --------------------------------------------------------------------------
# CONFIG  --  everything you'd want to tune lives here.
# --------------------------------------------------------------------------

BENCHMARK = "SPY"            # what "the market" means

LOOKBACK_DAYS = 60           # trading days used to build each stock's baseline
Z_THRESHOLD = 3.0            # how many std devs above normal counts as "far"
VOLUME_CONFIRM_MULT = 1.5    # today's volume must exceed this x median volume

MIN_PRICE = 5.0              # ignore sub-$5 stocks (penny-stock noise)
MIN_AVG_DOLLAR_VOLUME = 5_000_000   # ignore illiquid names (< $5M traded/day)

MAX_ALERTS = 15             # safety cap so a crazy market day can't spam you
HISTORY_PERIOD = "5mo"      # calendar history to pull per ticker

# --- rate-limit / caching knobs ---
PER_RUN_FETCH_CAP = 2200    # max tickers to fetch in one run (under Yahoo's limit)
DOWNLOAD_BATCH = 200        # tickers per yfinance call
SLEEP_BETWEEN_BATCHES = 1.5  # seconds to pause between batches (be polite)
MAX_BATCH_RETRIES = 3       # retries per batch on rate-limit/empty
SPY_RETRIES = 5             # the benchmark gets extra retries; the run needs it
CACHE_DIR = os.environ.get("CACHE_DIR", "cache")
CACHE_MAX_ROWS = 90         # trading days of history to keep in the cache

NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

# Note: an unset GitHub secret is passed as "" (empty), not absent, so a plain
# default in .get() wouldn't catch it. `or` handles both None and "".
NTFY_SERVER = (os.environ.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")


@dataclass
class Hit:
    ticker: str
    z: float
    stock_ret: float      # today's stock return (fraction)
    excess_ret: float     # today's return minus SPY's
    price: float
    dollar_vol: float     # average daily dollar volume over lookback


# --------------------------------------------------------------------------
# SIGNAL  --  pure function, unit-testable with synthetic data. (UNCHANGED)
# --------------------------------------------------------------------------

def evaluate_ticker(
    ticker: str,
    close: pd.Series,
    volume: pd.Series,
    spy_ret: pd.Series,
    *,
    lookback: int = LOOKBACK_DAYS,
    z_threshold: float = Z_THRESHOLD,
    vol_mult: float = VOLUME_CONFIRM_MULT,
    min_price: float = MIN_PRICE,
    min_dollar_vol: float = MIN_AVG_DOLLAR_VOLUME,
) -> Hit | None:
    """Return a Hit if `ticker` far-outperformed today, else None.

    `close` / `volume` are this stock's daily series (ascending dates).
    `spy_ret` is SPY's daily returns, indexed the same way.
    The most recent row is treated as "today"; the prior `lookback` rows
    form the baseline distribution (today is excluded from its own baseline).
    """
    close = close.dropna()
    volume = volume.reindex(close.index)
    if len(close) < lookback + 2:
        return None

    price = float(close.iloc[-1])
    if price < min_price:
        return None

    # Liquidity gate: average dollar volume over the lookback window.
    recent_dollar_vol = (close * volume).iloc[-lookback:]
    avg_dollar_vol = float(recent_dollar_vol.mean())
    if not np.isfinite(avg_dollar_vol) or avg_dollar_vol < min_dollar_vol:
        return None

    # Daily returns, aligned to SPY's calendar.
    stock_ret = close.pct_change()
    aligned = pd.concat({"s": stock_ret, "m": spy_ret}, axis=1).dropna()
    if len(aligned) < lookback + 2:
        return None

    excess = aligned["s"] - aligned["m"]
    today_excess = float(excess.iloc[-1])
    today_stock_ret = float(aligned["s"].iloc[-1])

    # Only care about *upside* outperformance.
    if today_excess <= 0:
        return None

    # Baseline = the lookback window ending the day BEFORE today.
    baseline = excess.iloc[-(lookback + 1):-1]
    mu = float(baseline.mean())
    sigma = float(baseline.std(ddof=1))
    if not np.isfinite(sigma) or sigma == 0:
        return None

    z = (today_excess - mu) / sigma
    if z < z_threshold:
        return None

    # Volume confirmation: today's volume well above its own median.
    med_vol = float(volume.iloc[-(lookback + 1):-1].median())
    today_vol = float(volume.iloc[-1])
    if not np.isfinite(med_vol) or med_vol <= 0:
        return None
    if today_vol < vol_mult * med_vol:
        return None

    return Hit(
        ticker=ticker,
        z=z,
        stock_ret=today_stock_ret,
        excess_ret=today_excess,
        price=price,
        dollar_vol=avg_dollar_vol,
    )


# --------------------------------------------------------------------------
# UNIVERSE
# --------------------------------------------------------------------------

def fetch_universe() -> list[str]:
    """Pull current NASDAQ + other-listed common stocks (ETFs/test issues out)."""
    tickers: set[str] = set()

    def parse(url: str, symbol_col: str):
        txt = requests.get(url, timeout=30).text
        lines = [ln for ln in txt.splitlines() if ln and "File Creation Time" not in ln]
        df = pd.read_csv(io.StringIO("\n".join(lines)), sep="|")
        df = df[df.get("Test Issue", "N") == "N"]
        if "ETF" in df.columns:
            df = df[df["ETF"] != "Y"]
        for raw in df[symbol_col].dropna().astype(str):
            sym = raw.strip().upper()
            if any(c in sym for c in ("$", "+", "=")) or not sym:
                continue
            sym = sym.replace(".", "-")
            if 1 <= len(sym) <= 6:
                tickers.add(sym)

    parse(NASDAQ_LISTED, "Symbol")
    parse(OTHER_LISTED, "NASDAQ Symbol")
    return sorted(tickers)


# --------------------------------------------------------------------------
# CACHE  (two wide DataFrames: closes and volumes, dates x tickers)
# --------------------------------------------------------------------------

def _cache_paths() -> tuple[str, str]:
    return (os.path.join(CACHE_DIR, "closes.parquet"),
            os.path.join(CACHE_DIR, "volumes.parquet"))


def load_cache() -> tuple[pd.DataFrame, pd.DataFrame]:
    cp, vp = _cache_paths()
    try:
        closes = pd.read_parquet(cp)
        volumes = pd.read_parquet(vp)
        closes.index = pd.to_datetime(closes.index)
        volumes.index = pd.to_datetime(volumes.index)
        print(f"  cache: {closes.shape[1]} tickers, {closes.shape[0]} days")
        return closes, volumes
    except Exception:
        print("  cache: empty (first run or not yet built)")
        return pd.DataFrame(), pd.DataFrame()


def save_cache(closes: pd.DataFrame, volumes: pd.DataFrame) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    cp, vp = _cache_paths()
    closes.to_parquet(cp)
    volumes.to_parquet(vp)
    print(f"  cache saved: {closes.shape[1]} tickers, {closes.shape[0]} days")


def merge_into_cache(closes, volumes, fetched: dict[str, pd.DataFrame]):
    """Fold freshly downloaded data into the cached wide frames (new wins)."""
    if not fetched:
        return closes, volumes
    nc = pd.DataFrame({t: df["Close"] for t, df in fetched.items()})
    nv = pd.DataFrame({t: df["Volume"] for t, df in fetched.items()})
    nc.index = pd.to_datetime(nc.index)
    nv.index = pd.to_datetime(nv.index)
    # combine_first: freshly fetched values take precedence; union of cols/dates.
    closes = nc.combine_first(closes) if not closes.empty else nc
    volumes = nv.combine_first(volumes) if not volumes.empty else nv
    closes = closes.sort_index().tail(CACHE_MAX_ROWS)
    volumes = volumes.sort_index().reindex(closes.index)
    return closes, volumes


# --------------------------------------------------------------------------
# DOWNLOAD  (throttled, retrying, partial-OK)
# --------------------------------------------------------------------------

def _download_once(chunk: list[str]) -> dict[str, pd.DataFrame]:
    data = yf.download(
        chunk, period=HISTORY_PERIOD, interval="1d", auto_adjust=True,
        group_by="ticker", threads=True, progress=False,
    )
    out: dict[str, pd.DataFrame] = {}
    if data is None or data.empty:
        return out
    for t in chunk:
        try:
            sub = data[t] if isinstance(data.columns, pd.MultiIndex) else data
            df = sub[["Close", "Volume"]].dropna(how="all")
            if not df.empty:
                out[t] = df
        except (KeyError, TypeError):
            continue
    return out


def download_prices(tickers: list[str]) -> dict[str, pd.DataFrame]:
    """Download in throttled batches; retry rate-limited batches; keep what we get."""
    out: dict[str, pd.DataFrame] = {}
    n_batches = (len(tickers) + DOWNLOAD_BATCH - 1) // DOWNLOAD_BATCH
    for bi in range(n_batches):
        chunk = tickers[bi * DOWNLOAD_BATCH:(bi + 1) * DOWNLOAD_BATCH]
        for attempt in range(MAX_BATCH_RETRIES):
            try:
                got = _download_once(chunk)
            except Exception as e:
                got = {}
                msg = str(e).lower()
                if "too many" not in msg and "rate" not in msg:
                    print(f"    batch {bi} error: {e}", file=sys.stderr)
            if got:
                out.update(got)
                break
            # nothing came back -> likely rate-limited; back off and retry
            time.sleep(5 * (attempt + 1) ** 2)
        time.sleep(SLEEP_BETWEEN_BATCHES)
        if (bi + 1) % 5 == 0:
            print(f"    {bi + 1}/{n_batches} batches, {len(out)} tickers so far")
    return out


def download_benchmark() -> pd.DataFrame | None:
    """Fetch SPY on its own, with extra retries. The run depends on it."""
    for attempt in range(SPY_RETRIES):
        got = {}
        try:
            got = _download_once([BENCHMARK])
        except Exception as e:
            print(f"  SPY attempt {attempt+1} error: {e}", file=sys.stderr)
        if BENCHMARK in got:
            return got[BENCHMARK]
        time.sleep(5 * (attempt + 1) ** 2)
    return None


# --------------------------------------------------------------------------
# FETCH-LIST SELECTION
# --------------------------------------------------------------------------

def is_liquid_in_cache(t, closes, volumes) -> bool:
    if closes.empty or t not in closes.columns:
        return False
    c = closes[t].dropna()
    if len(c) < LOOKBACK_DAYS or float(c.iloc[-1]) < MIN_PRICE:
        return False
    v = volumes[t].reindex(c.index)
    adv = float((c * v).tail(LOOKBACK_DAYS).mean())
    return np.isfinite(adv) and adv >= MIN_AVG_DOLLAR_VOLUME


def select_fetch_list(universe, closes, volumes, today_ordinal) -> list[str]:
    """Liquid names (must refresh daily) + a rotating slice of the rest."""
    liquid = [t for t in universe if is_liquid_in_cache(t, closes, volumes)]
    liquid_set = set(liquid)
    rest = [t for t in universe if t not in liquid_set]

    room = max(0, PER_RUN_FETCH_CAP - len(liquid))
    if rest and room:
        start = (today_ordinal * room) % len(rest)
        rotated = rest[start:] + rest[:start]
        discovery = rotated[:room]
    else:
        discovery = []

    fetch_list = (liquid + discovery)[:PER_RUN_FETCH_CAP]
    print(f"  fetch list: {len(liquid)} liquid + {len(discovery)} discovery "
          f"= {len(fetch_list)} (universe {len(universe)})")
    return fetch_list


# --------------------------------------------------------------------------
# ALERT
# --------------------------------------------------------------------------

def _clean_topic(raw: str) -> str:
    """Accept a bare topic ("abc123") OR a full URL pasted by mistake
    ("https://ntfy.sh/abc123") and return just the topic segment. A full URL in
    the secret is the usual cause of a 404 (the path ends up doubled)."""
    t = (raw or "").strip().strip("/")
    if "/" in t:
        t = t.split("/")[-1]
    return t


def send_alert(hits: list[Hit], as_of: str) -> None:
    lines = [
        f"{h.ticker}  +{h.stock_ret*100:.1f}%  "
        f"({h.excess_ret*100:+.1f}% vs SPY, z={h.z:.1f})  ${h.price:,.2f}"
        for h in hits
    ]
    body = "\n".join(lines)
    title = f"{len(hits)} stock(s) far outperforming -- {as_of}"
    print(f"\n{title}\n{body}")

    topic = _clean_topic(NTFY_TOPIC)
    if not topic:
        print("NTFY_TOPIC not set; printed above instead of pushing.", file=sys.stderr)
        return

    server = NTFY_SERVER if "://" in NTFY_SERVER else f"https://{NTFY_SERVER}"
    url = f"{server}/{topic}"
    masked = f"{server}/{topic[:3]}***"   # don't leak the full topic in logs

    # A delivery hiccup must NOT fail an otherwise-successful scan, so we log
    # the real reason instead of raising.
    try:
        resp = requests.post(
            url, data=body.encode("utf-8"),
            headers={"Title": title, "Priority": "default",
                     "Tags": "chart_with_upwards_trend"},
            timeout=30,
        )
        if resp.status_code >= 400:
            print(f"ntfy POST to {masked} failed: HTTP {resp.status_code} -> "
                  f"{resp.text[:200]}", file=sys.stderr)
            print("Fix: NTFY_TOPIC should be ONLY the topic name (e.g. "
                  "'stock-alerts-8fk39dlz'), not the full URL, and must match "
                  "what you subscribed to in the ntfy app.", file=sys.stderr)
        else:
            print(f"Pushed to {masked}")
    except Exception as e:
        print(f"ntfy POST error ({masked}): {e}", file=sys.stderr)


# --------------------------------------------------------------------------
# MAIN
# --------------------------------------------------------------------------

def main() -> int:
    print("Fetching universe...")
    universe = fetch_universe()
    print(f"  {len(universe)} candidate tickers")

    print("Loading cache...")
    closes, volumes = load_cache()

    print("Fetching benchmark (SPY) first...")
    spy_df = download_benchmark()
    if spy_df is None:
        print("ERROR: could not fetch SPY after retries; aborting this run.",
              file=sys.stderr)
        return 1
    closes, volumes = merge_into_cache(closes, volumes, {BENCHMARK: spy_df})

    spy_close = closes[BENCHMARK].dropna()
    spy_ret = spy_close.pct_change()
    as_of_ts = spy_close.index[-1]
    as_of = as_of_ts.date().isoformat()

    age_days = (dt.date.today() - as_of_ts.date()).days
    if age_days > 4:
        print(f"Latest data is {age_days} days old ({as_of}); likely a holiday. Skipping.")
        save_cache(closes, volumes)
        return 0

    print("Selecting fetch list...")
    fetch_list = select_fetch_list(universe, closes, volumes, as_of_ts.toordinal())
    fetch_list = [t for t in fetch_list if t != BENCHMARK]

    print("Downloading prices (throttled)...")
    fetched = download_prices(fetch_list)
    print(f"  fetched {len(fetched)} tickers this run")
    closes, volumes = merge_into_cache(closes, volumes, fetched)
    save_cache(closes, volumes)

    # Scan every cached ticker that has TODAY's bar.
    print("Scanning...")
    hits: list[Hit] = []
    eligible = 0
    for t in closes.columns:
        if t == BENCHMARK:
            continue
        col = closes[t]
        lvi = col.last_valid_index()
        if lvi is None or lvi != as_of_ts:   # no fresh bar today -> skip
            continue
        eligible += 1
        hit = evaluate_ticker(t, col, volumes[t], spy_ret)
        if hit:
            hits.append(hit)

    cov = (eligible / max(1, len(universe))) * 100
    print(f"  {eligible} tickers had today's bar (~{cov:.0f}% of universe), "
          f"{len(hits)} hit(s)")

    hits.sort(key=lambda h: h.z, reverse=True)
    if not hits:
        print("Nothing far-outperforming today. No notification sent.")
        return 0

    send_alert(hits[:MAX_ALERTS], as_of)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
