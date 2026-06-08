#!/usr/bin/env python3
"""
Whole-market end-of-day outperformance scanner.

Once a day (run from GitHub Actions), this:
  1. Fetches the full list of US common stocks (NASDAQ + NYSE/other listings).
  2. Downloads ~3 months of daily prices via yfinance.
  3. For each stock, computes today's return *in excess of SPY* and expresses
     it as a z-score against that stock's own recent excess-return distribution.
  4. Keeps only liquid names (price + dollar-volume gates) whose move is
     confirmed by above-average volume.
  5. Pushes the survivors to your phone via ntfy.

Nothing here needs an API key. The only secret is your ntfy topic name,
read from the environment (set as a GitHub Actions secret).
"""

from __future__ import annotations

import io
import os
import sys
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

MAX_ALERTS = 15              # safety cap so a crazy market day can't spam you
DOWNLOAD_CHUNK = 150         # tickers per yfinance request
HISTORY_PERIOD = "5mo"       # calendar history to pull (must exceed LOOKBACK)

NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
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
# SIGNAL  --  pure function, unit-testable with synthetic data.
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
# DATA  --  universe + prices.
# --------------------------------------------------------------------------

def fetch_universe() -> list[str]:
    """Pull current NASDAQ + other-listed common stocks (ETFs/test issues out)."""
    tickers: set[str] = set()

    def parse(url: str, symbol_col: str):
        txt = requests.get(url, timeout=30).text
        # Files are pipe-delimited with a trailing "File Creation Time" footer.
        lines = [ln for ln in txt.splitlines() if ln and "File Creation Time" not in ln]
        df = pd.read_csv(io.StringIO("\n".join(lines)), sep="|")
        df = df[df.get("Test Issue", "N") == "N"]
        if "ETF" in df.columns:
            df = df[df["ETF"] != "Y"]
        for raw in df[symbol_col].dropna().astype(str):
            sym = raw.strip().upper()
            # Skip preferreds/warrants/units; normalize class shares for yfinance.
            if any(c in sym for c in ("$", "+", "=")) or not sym:
                continue
            sym = sym.replace(".", "-")
            if 1 <= len(sym) <= 6:
                tickers.add(sym)

    parse(NASDAQ_LISTED, "Symbol")
    parse(OTHER_LISTED, "NASDAQ Symbol")
    return sorted(tickers)


def download_prices(tickers: list[str]) -> dict[str, pd.DataFrame]:
    """Batch-download daily OHLCV; returns {ticker: DataFrame[Close, Volume]}."""
    out: dict[str, pd.DataFrame] = {}
    for i in range(0, len(tickers), DOWNLOAD_CHUNK):
        chunk = tickers[i : i + DOWNLOAD_CHUNK]
        try:
            data = yf.download(
                chunk,
                period=HISTORY_PERIOD,
                interval="1d",
                auto_adjust=True,
                group_by="ticker",
                threads=True,
                progress=False,
            )
        except Exception as e:  # network hiccup on one chunk shouldn't kill the run
            print(f"  chunk {i//DOWNLOAD_CHUNK} failed: {e}", file=sys.stderr)
            continue
        if data is None or data.empty:
            continue
        for t in chunk:
            try:
                sub = data[t] if isinstance(data.columns, pd.MultiIndex) else data
                df = sub[["Close", "Volume"]].dropna(how="all")
                if not df.empty:
                    out[t] = df
            except (KeyError, TypeError):
                continue
    return out


# --------------------------------------------------------------------------
# ALERT
# --------------------------------------------------------------------------

def send_alert(hits: list[Hit], as_of: str) -> None:
    if not NTFY_TOPIC:
        print("NTFY_TOPIC not set; printing instead of pushing.", file=sys.stderr)
    lines = []
    for h in hits:
        lines.append(
            f"{h.ticker}  +{h.stock_ret*100:.1f}%  "
            f"({h.excess_ret*100:+.1f}% vs SPY, z={h.z:.1f})  ${h.price:,.2f}"
        )
    body = "\n".join(lines)
    title = f"{len(hits)} stock(s) far outperforming — {as_of}"
    print(f"\n{title}\n{body}")

    if not NTFY_TOPIC:
        return
    url = f"{NTFY_SERVER}/{NTFY_TOPIC}"
    resp = requests.post(
        url,
        data=body.encode("utf-8"),
        headers={
            "Title": title,
            "Priority": "default",
            "Tags": "chart_with_upwards_trend",
        },
        timeout=30,
    )
    resp.raise_for_status()
    print(f"Pushed to {url}")


# --------------------------------------------------------------------------
# MAIN
# --------------------------------------------------------------------------

def main() -> int:
    print("Fetching universe...")
    universe = fetch_universe()
    print(f"  {len(universe)} candidate tickers")
    # SPY is needed as the benchmark; make sure it's in the download set.
    to_download = sorted(set(universe) | {BENCHMARK})

    print("Downloading prices (this is the slow part)...")
    prices = download_prices(to_download)
    print(f"  got data for {len(prices)} tickers")

    if BENCHMARK not in prices:
        print(f"ERROR: no data for benchmark {BENCHMARK}; aborting.", file=sys.stderr)
        return 1

    spy_close = prices[BENCHMARK]["Close"].dropna()
    spy_ret = spy_close.pct_change()
    as_of = spy_close.index[-1].date().isoformat()

    # Freshness guard: don't re-alert on stale data (holidays, Yahoo lag).
    age_days = (dt.date.today() - spy_close.index[-1].date()).days
    if age_days > 3:
        print(f"Latest data is {age_days} days old ({as_of}); likely a holiday. Skipping.")
        return 0

    print("Scanning...")
    hits: list[Hit] = []
    for t, df in prices.items():
        if t == BENCHMARK:
            continue
        hit = evaluate_ticker(t, df["Close"], df["Volume"], spy_ret)
        if hit:
            hits.append(hit)

    hits.sort(key=lambda h: h.z, reverse=True)
    print(f"  {len(hits)} hit(s) before cap")

    if not hits:
        print("Nothing far-outperforming today. No notification sent.")
        return 0

    send_alert(hits[:MAX_ALERTS], as_of)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
