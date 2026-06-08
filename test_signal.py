import numpy as np
import pandas as pd
from scan import evaluate_ticker

rng = np.random.default_rng(42)
N = 120
dates = pd.bdate_range("2025-01-01", periods=N)

# SPY: small daily moves around 0.
spy_ret = pd.Series(rng.normal(0.0003, 0.008, N), index=dates)
spy_close = 400 * (1 + spy_ret).cumprod()

def make(close_ret, base_vol, last_vol=None):
    ret = pd.Series(close_ret, index=dates)
    close = 50 * (1 + ret).cumprod()
    vol = pd.Series(rng.normal(base_vol, base_vol * 0.1, N), index=dates).abs()
    if last_vol is not None:
        vol.iloc[-1] = last_vol
    return close, vol

# 1) Quiet stock that tracks SPY closely -> should NOT fire.
qr = spy_ret.values + rng.normal(0, 0.004, N)
q_close, q_vol = make(qr, 2_000_000)
print("quiet      ->", evaluate_ticker("QUIET", q_close, q_vol, spy_ret))

# 2) Liquid stock, normal all year, then a huge +14% spike today on 3x volume.
br = rng.normal(0.0002, 0.012, N)
br[-1] = 0.14
b_close, b_vol = make(br, 3_000_000, last_vol=9_000_000)
print("breakout   ->", evaluate_ticker("BREAK", b_close, b_vol, spy_ret))

# 3) Same big spike but ILLIQUID (tiny dollar volume) -> filtered out.
i_close, i_vol = make(br, 5_000)   # ~$50 * 5k = $250k/day, well under $5M
print("illiquid   ->", evaluate_ticker("ILLIQ", i_close, i_vol, spy_ret))

# 4) Big price spike but on NORMAL volume -> volume filter rejects it.
n_close, n_vol = make(br, 3_000_000)  # last vol left at ~normal
print("no-volume  ->", evaluate_ticker("NOVOL", n_close, n_vol, spy_ret))

# 5) Penny stock spike (price < $5) -> price filter rejects it.
p_close = 2.0 * (1 + pd.Series(br, index=dates)).cumprod()
_, p_vol = make(br, 3_000_000, last_vol=9_000_000)
print("penny      ->", evaluate_ticker("PENNY", p_close, p_vol, spy_ret))
