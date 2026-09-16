"""Which trend filter actually keeps the fade out of trouble?

All three live losses so far were SELLs that got run over while gold climbed, so
the M15 EMA50/200 gate the scalper uses is the prime suspect.  This sweeps the
alternatives on the same setup universe and reports PF / R per day / max drawdown.

    python fibo_trend_study.py
"""
from __future__ import annotations

import sys

import numpy as np

import fiboscalper as S
import fibo_ha_backtest as F
from mt5lib import connect, ema

PT = 0.01


def runs(arr) -> float:
    """Worst peak-to-trough of the cumulative point curve."""
    c = np.cumsum(np.array([t.pts for t in arr]))
    return float((np.maximum.accumulate(c) - c).max()) if c.size else 0.0


def line(tag, ts, days):
    if not ts:
        print("  %-42s (tiada trade)" % tag)
        return
    p = np.array([t.pts for t in ts])
    rk = np.array([t.risk_pts for t in ts])
    rr = p / np.maximum(rk, 1e-9)
    w, l = p[p > 0].sum(), abs(p[p <= 0].sum())
    print("  %-42s n=%4d win=%4.1f%% PF=%.2f E[R]=%+.3f R/hari=%+.2f DD=%6.0fpt"
          % (tag, len(p), 100 * (p > 0).mean(), w / l if l else float("inf"),
             rr.mean(), rr.mean() * len(p) / days, runs(ts)))


def main() -> int:
    connect()
    d = S.load(S.M5, 20000)
    ho, hh, hl, hc = F.heikin_ashi(d)
    t5 = np.asarray(d["time"], dtype=np.int64)
    days = len(hc) * 5 / 60 / 24
    import MetaTrader5 as mt5

    K = dict(tp_level=100.0, zone=23.6, gap_zone=38.2, sl_buf=200, min_ext_atr=1.0,
             grace_bars=2, sequential=True, cycle_lock=True, lo_len=1, hi_len=12)

    print("=" * 100)
    print("TREND FILTER STUDY - which gate keeps the fade alive? (20,000 M5 bars, %.0f days)" % days)
    print("=" * 100)

    m15 = F.htf_trend(t5, mt5.TIMEFRAME_M15)
    h1 = F.htf_trend(t5, mt5.TIMEFRAME_H1)
    m5 = np.where(ema(d["close"], 50) > ema(d["close"], 200), 1, -1)

    print("\n--- GATE VARIATIONS (trend_mode=align: only fade WITH the higher trend) ---")
    line("tiada filter (semua setup)", F.run(d, ho, hh, hl, hc, trend=None, trend_mode="off", **K), days)
    line("M15 EMA50/200 (sekarang)", F.run(d, ho, hh, hl, hc, trend=m15, trend_mode="align", **K), days)
    line("H1 EMA50/200", F.run(d, ho, hh, hl, hc, trend=h1, trend_mode="align", **K), days)
    line("M5 EMA50/200", F.run(d, ho, hh, hl, hc, trend=m5, trend_mode="align", **K), days)

    print("\n--- ARAH: adakah SELL sahaja yang cedera? ---")
    sell_only = [t for t in F.run(d, ho, hh, hl, hc, trend=None, trend_mode="off", **K) if t.side == "SELL"]
    buy_only = [t for t in F.run(d, ho, hh, hl, hc, trend=None, trend_mode="off", **K) if t.side == "BUY"]
    line("semua SELL (fade group bullish)", sell_only, days)
    line("semua BUY (fade group bearish)", buy_only, days)

    print("\n--- PENAPIS TAMBAHAN: jangan fade kalau harga baru buat extreme baru ---")
    hi_run = np.full(len(hc), 1)
    lo_run = np.full(len(hc), -1)
    for i in range(20, len(hc)):
        hi_run[i] = 1 if hh[i] > hh[i - 20:i].max() else -1     # new 100-min high
        lo_run[i] = 1 if hl[i] < hl[i - 20:i].min() else -1     # new 100-min low
    # for a SELL we need the tape NOT to be making new highs -> allow only where hi_run == -1
    both = np.where(hi_run == 1, 1, lo_run)                     # +1 = making highs, -1 = making lows
    line("elak fade high baru (100 min)", F.run(d, ho, hh, hl, hc, trend=both, trend_mode="align", **K), days)

    print("\n--- SELARI: trend M15 + elak fade extreme baru ---")
    comb = np.where(m15 == 1, 1, np.where(hi_run == 1, 1, -1))
    line("M15 align + elak high baru", F.run(d, ho, hh, hl, hc, trend=comb, trend_mode="align", **K), days)

    print("=" * 100)
    return 0


if __name__ == "__main__":
    sys.exit(main())
