"""Teacher's layered-entry / multi-TP rules, tested again.

His refinements (2026-09-16):
  * enter FAST - the group-end flip candle closes, draw the fibo, fire as soon
    as price touches the zone
  * sideways tape  -> all targets at 100.0, but scale in over several layers
  * clear trend    -> split the target: part at 100.0, part at 161.8 (and 261.8)

Setup universe comes straight from fibo_ha_backtest.run() so the detection is
identical to the live scalper.  Each setup is then RE-SIMULATED with different
entry ladders and target ladders.  Regime is ADX(14) on M5 - the same
indicator the teacher keeps on his chart.

    python fibo_layers_backtest.py
"""
from __future__ import annotations

import sys
from dataclasses import dataclass

import numpy as np

import fiboscalper as S
import fibo_ha_backtest as F
from mt5lib import adx as mt5lib_adx, connect

PT = 0.01          # XAUUSD: 1 point = 0.01
SPREAD = 7.0       # points paid per fill
HOLD = 200         # bars a cycle may run


# ------------------------------------------------------------------ ADX(14)
def adx(d, n: int = 14) -> np.ndarray:
    """Wilder ADX(14) on the raw M5 bars. 0 until it warms up.

    Thin adapter over mt5lib.adx - one implementation, not two.
    """
    a, _, _ = mt5lib_adx(d["high"], d["low"], d["close"], n)
    return np.nan_to_num(a, nan=0.0)


# ------------------------------------------------------------------ sim
@dataclass
class Layered:
    pts: float        # net points, weight-normalised (sum of weights == 1)
    risk: float       # points from the weighted entry to the stop
    fills: int        # how many layers actually got filled
    regime: str       # tape at entry: SIDE / TREND
    side: str


def simulate(t: F.Trade, hi_a, lo_a, cl_a, *, layers, tps, win=3,
             sl_pts=200.0, regime="SIDE", spread=SPREAD, first_touch=False,
             zone=23.6, be_trigger=None, be_level=100.0):
    """Re-run one setup with an entry ladder and a target ladder.

    layers : fib % to scale into, e.g. (0.0, 11.8, 23.6) for a SELL - each is
             weighted equally.  A layer fills if price trades through it within
             `win` bars of the group end.
    tps    : ((weight, fib%), ...) - each cluster leaves at its own level.
    first_touch : fill the whole position at the first bar that trades inside
             0..zone - this is what the live scalper actually does, so it is the
             honest baseline to measure a ladder against.
    """
    p0, p100 = t.p0, t.p100
    px = lambda L: p0 + (p100 - p0) * L / 100.0
    sell = t.side == "SELL"
    sl = p0 + sl_pts * PT if sell else p0 - sl_pts * PT

    # --- entry ladder: fill a layer when price touches its level
    got, wsum, cost = 0, 0.0, 0.0
    w = 1.0 / len(layers)
    if first_touch:
        lo_lvl, hi_lvl = px(zone), px(0.0)
        if lo_lvl > hi_lvl:
            lo_lvl, hi_lvl = hi_lvl, lo_lvl
        for k in range(t.i_entry, min(t.i_entry + win, cl_a.size)):
            if lo_lvl <= hi_a[k] and lo_a[k] <= hi_lvl:      # traded inside the zone
                cost, wsum, got = t.entry, 1.0, 1
                break
    else:
        for L in layers:
            p = px(L)
            for k in range(t.i_entry, min(t.i_entry + win, cl_a.size)):
                hit = (hi_a[k] >= p) if sell else (lo_a[k] <= p)
                if hit:
                    cost += w * p
                    wsum += w
                    got += 1
                    break
    if wsum == 0:
        return None                      # never traded into the zone
    entry = cost / wsum

    # --- exit: each target cluster leaves at its level, stop kills the rest.
    # be_trigger / be_level implement the teacher's runner rule: once price has
    # cleared 161.8 and is heading for 261.8, drag the stop up to the 100.0 line
    # so the runner cannot turn into a loser.
    left, pts, stop, armed = 1.0, 0.0, sl, (be_trigger is None)
    be_px = px(be_level) if be_trigger is not None else 0.0
    trig_px = px(be_trigger) if be_trigger is not None else 0.0
    for k in range(t.i_entry, min(t.i_entry + HOLD, cl_a.size)):
        if not armed:
            hit = (lo_a[k] <= trig_px) if sell else (hi_a[k] >= trig_px)
            if hit:
                armed, stop = True, be_px
        for wgt, L in tps:
            if left <= 1e-9:
                break
            tp = px(L)
            hit = (lo_a[k] <= tp) if sell else (hi_a[k] >= tp)
            if hit:
                q = min(wgt, left)
                pts += q * ((entry - tp) if sell else (tp - entry)) / PT
                left -= q
        if left <= 1e-9:
            break
        dead = (hi_a[k] >= stop) if sell else (lo_a[k] <= stop)
        if dead:
            pts += left * ((entry - stop) if sell else (stop - entry)) / PT
            left = 0.0
            break
    if left > 1e-9:                      # ran out of bars: mark to market
        k = min(t.i_entry + HOLD, cl_a.size - 1)
        pts += left * ((entry - cl_a[k]) if sell else (cl_a[k] - entry)) / PT

    return Layered(pts - spread, abs(entry - sl) / PT, got, regime, t.side)


def stats(rows, days):
    if not rows:
        return "  (tiada setup)"
    p = np.array([r.pts for r in rows])
    rk = np.array([r.risk for r in rows])
    rr = p / np.maximum(rk, 1e-9)
    wins, loss = p[p > 0].sum(), abs(p[p <= 0].sum())
    return ("n=%4d  win=%5.1f%%  PF=%.2f  exp=%+7.2fpt  E[R]=%+.3f  R/hari=%+.2f"
            % (len(p), 100 * (p > 0).mean(), wins / loss if loss else float("inf"),
               p.mean(), rr.mean(), rr.mean() * len(p) / days))


def main() -> int:
    connect()
    d = S.load(S.M5, 20000)
    ho, hh, hl, hc = F.heikin_ashi(d)
    t5 = np.asarray(d["time"], dtype=np.int64)
    hi_a, lo_a, cl_a = hh, hl, hc            # the HA candle the teacher reads
    trend = F.htf_trend(t5, __import__("MetaTrader5").TIMEFRAME_M15)
    ax = adx(d)
    days = len(hc) * 5 / 60 / 24

    K = dict(tp_level=100.0, zone=23.6, gap_zone=38.2, sl_buf=200, min_ext_atr=1.0,
             grace_bars=2, sequential=True, trend=trend, trend_mode="align",
             cycle_lock=True, lo_len=1, hi_len=12)
    base = F.run(d, ho, hh, hl, hc, **K)
    print("=" * 100)
    print("SETUP UNIVERSE: %d setups dari 20,000 bar M5 (%.0f hari)" % (len(base), days))
    print("ADX(14) pada bar entry: median %.1f | 25%% %.1f | 75%% %.1f"
          % (np.median(ax[[t.i_entry for t in base]]),
             np.percentile(ax[[t.i_entry for t in base]], 25),
             np.percentile(ax[[t.i_entry for t in base]], 75)))
    print("=" * 100)

    def run_case(tag, layers, tps, cut=None):
        rows = [r for t in base
                if (cut is None or cut(ax[t.i_entry]))
                for r in [simulate(t, hi_a, lo_a, cl_a, layers=layers, tps=tps)] if r]
        print("  %-46s %s" % (tag, stats(rows, days)))
        return rows

    SINGLE = (0.0,)
    MID = (11.8,)
    LADDER3 = (0.0, 11.8, 23.6)
    T100 = ((1.0, 100.0),)
    SPLIT = ((0.5, 100.0), (0.5, 161.8))
    SPLIT3 = ((0.4, 100.0), (0.3, 161.8), (0.3, 261.8))

    def run_case(tag, layers, tps, ft=False, be=None, cut=None):
        rows = [r for t in base
                if (cut is None or cut(ax[t.i_entry]))
                for r in [simulate(t, hi_a, lo_a, cl_a, layers=layers, tps=tps,
                                   first_touch=ft,
                                   be_trigger=be[0] if be else None,
                                   be_level=be[1] if be else 100.0)] if r]
        print("  %-50s %s" % (tag, stats(rows, days)))
        return rows

    print("\n--- BASELINE: cara scalper live sekarang (masuk sentuhan pertama zon) ---")
    run_case("first-touch 0-23.6, TP tunggal 100", MID, T100, ft=True)
    run_case("first-touch 0-23.6, split TP 100/161.8", MID, SPLIT, ft=True)

    print("\n--- TIP 1 KAU: breakeven ke 100 selepas 161.8 ---")
    run_case("first-touch, split, BE@100 selepas 161.8", MID, SPLIT, ft=True, be=(161.8, 100.0))
    run_case("first-touch, split, BE@100 selepas 261.8", MID, SPLIT, ft=True, be=(261.8, 100.0))
    run_case("first-touch, 40/30/30 kt 100/161.8/261.8, BE@100 lepas 161.8",
             MID, SPLIT3, ft=True, be=(161.8, 100.0))

    print("\n--- TIP 2 KAU: layer dalam zon 0-23.6 ---")
    run_case("3 layer 0/11.8/23.6, TP 100", LADDER3, T100)
    run_case("3 layer 0/11.8/23.6, split 100/161.8", LADDER3, SPLIT)
    run_case("3 layer + split + BE@100 lepas 161.8", LADDER3, SPLIT, be=(161.8, 100.0))
    run_case("5 layer (0/5.9/11.8/17.7/23.6), split", (0, 5.9, 11.8, 17.7, 23.6), SPLIT)

    print("\n--- REGIME: sideway vs trend (ADX) ---")
    for tag, cut in (("SIDEWAY ADX<20", lambda a: a < 20), ("TREND ADX>=25", lambda a: a >= 25)):
        print("  %s:" % tag)
        run_case("    first-touch + TP 100", MID, T100, ft=True, cut=cut)
        run_case("    3 layer + split + BE", LADDER3, SPLIT, be=(161.8, 100.0), cut=cut)
    print("=" * 100)
    return 0


if __name__ == "__main__":
    sys.exit(main())
