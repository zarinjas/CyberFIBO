"""
Backtest: Fibonacci-retracement fade on Heikin Ashi groups (XAUUSDc M5).

The rule, as taught:
  1. Work on Heikin Ashi M5.
  2. Find a GROUP of consecutive same-colour HA candles.
  3. Draw the fibo over that group with 0.0 at the END of the group (where
     price is when the group finishes) and 100.0 at the START of the group.
  4. The technique only arms once the NEXT candle flips colour (group ended).
  5. Enter OPPOSITE to the group, but only if entry sits inside the 0.0-23.6
     zone.  That is what keeps the stop small.
  6. TP back at level 100.0 (the origin of the group) -- or an extension
     (161.8 / 261.8) when confident.
  7. SL just beyond level 0.0 (beyond the group's extreme).

`--variant start0` also tests the literal reading where 0.0 sits at the START
of the group instead; the data decides which one is the real edge.

    python fibo_ha_backtest.py
    python fibo_ha_backtest.py --variant start0
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass

import numpy as np

from mt5lib import M5, SYMBOL, bars, connect

PT = 0.01           # 1 point
SPREAD_PTS = 7      # measured on this broker


def heikin_ashi(d) -> tuple[np.ndarray, ...]:
    o = np.asarray(d["open"], dtype=float)
    h = np.asarray(d["high"], dtype=float)
    lo = np.asarray(d["low"], dtype=float)
    c = np.asarray(d["close"], dtype=float)
    n = len(c)
    ha_c = (o + h + lo + c) / 4.0
    ha_o = np.empty(n)
    ha_o[0] = o[0]
    for i in range(1, n):
        ha_o[i] = (ha_o[i - 1] + ha_c[i - 1]) / 2.0
    ha_h = np.maximum(h, np.maximum(ha_o, ha_c))
    ha_l = np.minimum(lo, np.minimum(ha_o, ha_c))
    return ha_o, ha_h, ha_l, ha_c


def groups(ha_o, ha_h, ha_l, ha_c, lo_len, hi_len):
    """Yield (start_idx, end_idx, bullish) for each same-colour run.

    Scans from index 0 so a run that starts at the very first bar is not
    truncated (starting at 1 silently dropped bar 0 of that run).
    """
    n = len(ha_c)
    bull = ha_c >= ha_o
    i = 0
    while i < n - 1:
        j = i
        while j + 1 < n and bull[j + 1] == bull[i]:
            j += 1
        ln = j - i + 1
        if lo_len <= ln <= hi_len:
            yield i, j, bool(bull[i])
        i = j + 1


@dataclass
class Trade:
    i_entry: int
    side: str            # BUY / SELL
    entry: float
    sl: float
    tp: float
    i_exit: int
    exit: float
    pts: float
    reason: str
    risk_pts: float = 0.0    # |entry - sl| in points, for risk-normalised stats
    lvl_entry: float = 0.0   # where we entered, as a fib %
    p0: float = 0.0          # the fib anchors, so a setup can be re-simulated
    p100: float = 0.0        # with different entries / targets


def htf_trend(m5_t: np.ndarray, tf: int, fast: int = 50, slow: int = 200) -> np.ndarray:
    """Regime per M5 bar: +1 uptrend, -1 downtrend, 0 unknown.

    Uses the higher timeframe's EMA fast vs slow, and only the HTF bars whose
    close is ALREADY complete at that M5 bar, so there is no lookahead.
    """
    import MetaTrader5 as mt5
    from mt5lib import ema as _ema
    step = {mt5.TIMEFRAME_M5: 300, mt5.TIMEFRAME_M15: 900,
            mt5.TIMEFRAME_M30: 1800, mt5.TIMEFRAME_H1: 3600}.get(tf, 900)
    # enough HTF bars to cover the whole M5 span, plus warm-up for the slow EMA
    need = int((m5_t[-1] - m5_t[0]) / step) + slow + 50
    r = mt5.copy_rates_from_pos(SYMBOL, tf, 0, min(max(need, 500), 50000))
    ht = r["time"].astype(np.int64)
    hc = r["close"].astype(float)
    up = np.where(_ema(hc, fast) > _ema(hc, slow), 1, -1)
    # last HTF bar fully closed strictly before this M5 bar's open
    idx = np.searchsorted(ht, m5_t - step, side="right") - 1
    return np.where(idx >= 0, up[np.clip(idx, 0, len(up) - 1)], 0)


def run(d, ha_o, ha_h, ha_l, ha_c, *, variant="end0", tp_level=100.0,
        lo_len=4, hi_len=12, zone=23.6, sl_buf=50, max_hold=200,
        slip_pts=0.0, sequential=False, invalidate=True, inv_close=True,
        grace_bars=0, trend=None, trend_mode="align", cycle_lock=False,
        gap_zone=None, min_ext_atr=0.0, rsi_gate=0.0, reentry=0.0,
        start=250, end=None, fill="open"):
    """gap_zone > zone lets a gapped entry in further out (up to 61.8) -- the
    wider stop is then paid for by a smaller lot (see r_* stats).
    min_ext_atr: require the leg's 0.0 line to sit at least k x ATR away from
    the M5 EMA20, i.e. only fade something that is actually stretched.
    rsi_gate: require RSI(M5,14) at entry to be at least this extreme.

    fill: "open" (default) fills at the OPEN of the bar we decided on. That price
    is ALREADY GONE by the time a live bot reaches the market - our own bot acts
    ~30s into the bar. It is not lookahead, but it is not achievable either, and
    it flatters results badly: on the same 1,408 setups the fibo 100.0 exit shows
    PF 12.14 / 93% win at fill="open" and PF 1.20 / 62% win at fill="close" (the
    trigger bar's close, which a bot can actually get). USE fill="close" FOR
    ANYTHING THAT INFORMS A DECISION; "open" survives only for comparability with
    the earlier numbers."""
    """invalidate: a candle breaking back out through the 0.0 line voids the
    setup (that is the primary exit; the SL is only the backstop).  grace_bars
    skips both SL and invalidation for the first N bars, so two candles of
    gold noise cannot stop the trade out before it has room to run.
    inv_close: judge the break on the bar CLOSE rather than the wick."""
    n = len(ha_c)
    end = end or n - 1
    out: list[Trade] = []
    busy_until = -1
    if gap_zone is None:            # default: no gapped entry, same as the normal zone
        gap_zone = zone
    o = np.asarray(d["open"], float)
    hi_a = np.asarray(d["high"], float)
    lo_a = np.asarray(d["low"], float)
    cl_a = np.asarray(d["close"], float)

    # filters that need their own indicator series (all causal)
    from mt5lib import atr as _atr, ema as _ema
    _ema20 = _ema(cl_a, 20)
    _atr14 = _atr(hi_a, lo_a, cl_a, 14)
    _d = np.diff(cl_a, prepend=cl_a[0])
    _up = np.where(_d > 0, _d, 0.0)
    _dn = np.where(_d < 0, -_d, 0.0)
    _au = np.empty(n); _ad = np.empty(n)
    _au[0] = _ad[0] = 0.0
    for _i in range(1, n):
        _au[_i] = (_au[_i - 1] * 13 + _up[_i]) / 14
        _ad[_i] = (_ad[_i - 1] * 13 + _dn[_i]) / 14
    _rsi = np.where(_ad > 0, 100 - 100 / (1 + _au / np.maximum(_ad, 1e-12)), 100.0)

    for gs, ge, bullish in groups(ha_o, ha_h, ha_l, ha_c, lo_len, hi_len):
        i_entry = ge + 1
        if i_entry >= end or gs < start:
            continue
        if sequential and i_entry <= busy_until:
            continue

        # anchors: 'end0' -> 0.0 at the group END; 'start0' -> 0.0 at the START
        if bullish:
            ext_start, ext_end = ha_l[gs:ge + 1].min(), ha_h[gs:ge + 1].max()
        else:
            ext_start, ext_end = ha_h[gs:ge + 1].max(), ha_l[gs:ge + 1].min()
        p0, p100 = (ext_end, ext_start) if variant == "end0" else (ext_start, ext_end)
        if abs(p100 - p0) < 1e-9:
            continue

        side = "SELL" if bullish else "BUY"          # fade the group
        # See the `fill` note in the docstring. o[i_entry] is unreachable in
        # practice and inflated every early study - most of the edge measured
        # here was the entry bar's own move, which the bot never sees.
        entry = (o if fill == "open" else cl_a)[i_entry]

        # ---- step 2: trend filter. 'align' trades only with the HTF trend,
        #      'counter' only against it, 'both' ignores the trend.
        if trend is not None and trend_mode != "both":
            want = 1 if side == "BUY" else -1
            tt = int(trend[i_entry])
            if tt == 0:
                continue
            if trend_mode == "align" and tt != want:
                continue
            if trend_mode == "counter" and tt == want:
                continue
        lvl = (entry - p0) / (p100 - p0) * 100.0      # where we enter, in fib %
        if not (0.0 <= lvl <= gap_zone):
            continue

        # --- only fade legs that are genuinely stretched away from the mean.
        # Indicators are read at i_entry-1: we act on the OPEN of i_entry, so
        # bar i_entry's own close is not yet known (reading it would be
        # lookahead).
        j = max(i_entry - 1, 0)
        if min_ext_atr > 0:
            a_ = _atr14[j]
            if not (a_ > 0) or abs(p0 - _ema20[j]) / a_ < min_ext_atr:
                continue

        # --- require RSI to be at an extreme (avoid middling entries)
        if rsi_gate > 0:
            r_ = _rsi[j]
            if side == "SELL" and r_ < 100 - rsi_gate:
                continue
            if side == "BUY" and r_ > rsi_gate:
                continue

        px = (lambda L: p0 + (p100 - p0) * L / 100.0)
        tp = px(tp_level)
        sl = px(-sl_buf / ((abs(p100 - p0) / PT) or 1) * 100.0)  # buffer below 0.0
        if side == "SELL" and not (tp < entry < sl):
            continue
        if side == "BUY" and not (sl < entry < tp):
            continue

        # ---- walk forward
        exit_px, i_ex, reason = None, None, None
        for k in range(i_entry, min(i_entry + max_hold, end + 1)):
            hi, lo = hi_a[k], lo_a[k]
            room = (k - i_entry) >= grace_bars      # grace: 2 candles of slack

            if room:                                # backstop stop
                if side == "SELL" and hi >= sl:
                    exit_px, i_ex, reason = sl + slip_pts * PT, k, "SL"; break
                if side == "BUY" and lo <= sl:
                    exit_px, i_ex, reason = sl - slip_pts * PT, k, "SL"; break

            if side == "SELL":                      # target
                if lo <= tp:
                    exit_px, i_ex, reason = tp, k, "TP"; break
            else:
                if hi >= tp:
                    exit_px, i_ex, reason = tp, k, "TP"; break

            if invalidate and room:                 # 0.0 line broken => void
                broke = ((cl_a[k] > p0) if inv_close else (hi >= p0)) if side == "SELL" \
                    else ((cl_a[k] < p0) if inv_close else (lo <= p0))
                if broke:
                    exit_px, i_ex, reason = cl_a[k], k, "INVALID"; break
        if exit_px is None:
            i_ex = min(i_entry + max_hold, end)
            exit_px, reason = cl_a[i_ex], "TIME"

        gross = (entry - exit_px) if side == "SELL" else (exit_px - entry)
        net = gross / PT - SPREAD_PTS
        out.append(Trade(i_entry, side, entry, sl, tp, i_ex, exit_px, net, reason,
                         risk_pts=abs(entry - sl) / PT, lvl_entry=lvl,
                         p0=p0, p100=p100))

        # ---- re-entry (the teacher's "ENTRY" label): while the cycle is still
        #      live, a retracement back to the ENTRY level is a second entry.
        if reentry > 0:
            rp = px(reentry)
            crossed = False
            for k in range(i_entry, i_ex + 1):
                if side == "SELL" and lo_a[k] <= rp:
                    crossed = True
                if side == "BUY" and hi_a[k] >= rp:
                    crossed = True
                if crossed and k >= i_entry:
                    # enter at the ENTRY level on this bar, same stops/target
                    for j in range(k, min(k + max_hold, end + 1)):
                        h2, l2 = hi_a[j], lo_a[j]
                        r2, x2 = None, None
                        if j - k >= grace_bars:
                            if side == "SELL" and h2 >= sl:
                                r2, x2 = "SL", sl + slip_pts * PT
                            elif side == "BUY" and l2 <= sl:
                                r2, x2 = "SL", sl - slip_pts * PT
                        if r2 is None:
                            if side == "SELL" and l2 <= tp:
                                r2, x2 = "TP", tp
                            elif side == "BUY" and h2 >= tp:
                                r2, x2 = "TP", tp
                        if r2 is None and invalidate and (j - k) >= grace_bars:
                            brk = (cl_a[j] > p0) if side == "SELL" else (cl_a[j] < p0)
                            if brk:
                                r2, x2 = "INVALID", cl_a[j]
                        if r2 is not None:
                            g2 = (rp - x2) if side == "SELL" else (x2 - rp)
                            out.append(Trade(k, side, rp, sl, tp, j, x2,
                                             g2 / PT - SPREAD_PTS, "RE-" + r2,
                                             risk_pts=abs(rp - sl) / PT,
                                             lvl_entry=reentry))
                            break
                    break

        if cycle_lock:
            # The fibo cycle stays LIVE until price either reaches 161.8
            # (cycle complete) or breaks back through 0.0 (setup void).
            # No new setup may start while the cycle is still running.
            far = px(161.8)
            lock = min(i_ex + max_hold, end)
            for k in range(i_ex, min(i_ex + max_hold, end + 1)):
                hit_far = (hi_a[k] >= far) if side == "SELL" else (lo_a[k] <= far)
                broke = (hi_a[k] >= p0) if side == "SELL" else (lo_a[k] <= p0)
                if hit_far or broke:
                    lock = k
                    break
            busy_until = max(busy_until, lock)
        else:
            busy_until = i_ex
    return out


def report(tag: str, ts: list[Trade]):
    if not ts:
        print(f"  {tag:<34} no trades")
        return None
    p = np.array([t.pts for t in ts])
    wins, losses = p[p > 0], p[p <= 0]
    pf = wins.sum() / abs(losses.sum()) if losses.sum() else float("inf")
    eq = np.cumsum(p)
    dd = (np.maximum.accumulate(eq) - eq).max()
    # risk-normalised: every trade sized to risk the same money
    rk = np.array([t.risk_pts for t in ts])
    r = p / np.maximum(rk, 1e-9)
    print(f"  {tag:<34} n={len(p):<5} win={len(wins)/len(p)*100:5.1f}%  "
          f"avgW={wins.mean() if len(wins) else 0:7.0f}  avgL={losses.mean() if len(losses) else 0:7.0f}  "
          f"exp={p.mean():7.1f}pt  PF={pf:5.2f}  tot={p.sum():8.0f}pt  maxDD={dd:7.0f}pt  "
          f"| risk={rk.mean():5.0f}pt  E[R]={r.mean():+.3f}")
    return dict(n=len(p), win=len(wins) / len(p), exp=p.mean(), pf=pf, tot=p.sum(),
                er=r.mean(), risk=rk.mean(), maxdd=dd)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", choices=["end0", "start0"], default="end0")
    ap.add_argument("--tp", type=float, default=None)
    ap.add_argument("--lo-len", type=int, default=4)
    ap.add_argument("--hi-len", type=int, default=12)
    ap.add_argument("--zone", type=float, default=23.6)
    ap.add_argument("--sl-buf", type=float, default=50)
    ap.add_argument("--slip", type=float, default=0.0)
    ap.add_argument("--grace", type=int, default=0)
    ap.add_argument("--no-invalidate", action="store_true")
    ap.add_argument("--no-inv-close", action="store_true")
    a = ap.parse_args()

    ti, ai = connect()
    d = bars(M5, 20000)
    ha_o, ha_h, ha_l, ha_c = heikin_ashi(d)
    nb = len(ha_c)
    K = dict(variant=a.variant, lo_len=a.lo_len, hi_len=a.hi_len, zone=a.zone,
             sl_buf=a.sl_buf, slip_pts=a.slip, invalidate=not a.no_invalidate,
             inv_close=not a.no_inv_close, grace_bars=a.grace)
    print("=" * 108)
    print(f"FIBO + HEIKIN ASHI FADE BACKTEST   {SYMBOL} M5   bars={nb} "
          f"({nb * 5 / 60 / 24:.0f} days)   spread={SPREAD_PTS}pt charged, slip={a.slip}pt on SL")
    print(f"variant={a.variant} (0.0 at group {'END' if a.variant=='end0' else 'START'})  "
          f"group len {a.lo_len}-{a.hi_len}  entry zone 0-{a.zone}%  SL buffer {a.sl_buf}pt")
    print("=" * 108)

    if a.tp is not None:
        report(f"TP {a.tp}", run(d, ha_o, ha_h, ha_l, ha_c, tp_level=a.tp, **K))
    else:
        print("\n-- 1. TP sweep (baseline) ----------------------------------------------")
        for tp in (23.6, 38.2, 50.0, 61.8, 78.6, 100.0, 161.8, 261.8):
            report(f"TP {tp}", run(d, ha_o, ha_h, ha_l, ha_c, tp_level=tp, **K))

        print("\n-- 2. ROBUSTNESS: SL buffer sweep at TP 100 (slip 0) --------------------")
        for b in (25, 50, 100, 200, 400, 800):
            report(f"SL buf {b}pt", run(d, ha_o, ha_h, ha_l, ha_c, tp_level=100.0,
                                        **{**K, "sl_buf": b}))

        print("\n-- 3. ROBUSTNESS: worst-case slippage on SL, at TP 100, buf 100 ---------")
        for s in (0, 20, 50, 100, 200):
            report(f"slip {s}pt", run(d, ha_o, ha_h, ha_l, ha_c, tp_level=100.0,
                                      **{**K, "sl_buf": 100, "slip_pts": s}))

        print("\n-- 4. Entry zone sweep at TP 100, len 6-10 ------------------------------")
        for z in (11.8, 23.6, 38.2, 50.0):
            report(f"zone 0-{z}%", run(d, ha_o, ha_h, ha_l, ha_c, tp_level=100.0,
                                       **{**K, "lo_len": 6, "hi_len": 10, "zone": z}))

        print("\n-- 5. Group length sweep at TP 100 --------------------------------------")
        for lo, hi in ((2, 30), (3, 30), (4, 12), (5, 10), (6, 10), (8, 12)):
            report(f"len {lo}-{hi}", run(d, ha_o, ha_h, ha_l, ha_c, tp_level=100.0,
                                         **{**K, "lo_len": lo, "hi_len": hi}))

        # walk-forward: is the edge one regime or does it persist?
        h = nb // 2
        print(f"\n-- 6. Walk-forward split (TP 100, buf {a.sl_buf}) -----------------------")
        for tag, s, e in (("H1 (older)", 250, h), ("H2 (newer)", h, nb - 1), ("FULL", 250, nb - 1)):
            report(tag, run(d, ha_o, ha_h, ha_l, ha_c, tp_level=100.0, start=s, end=e, **K))

        s = run(d, ha_o, ha_h, ha_l, ha_c, tp_level=100.0, **K)
        longs = [t for t in s if t.side == "BUY"]
        shorts = [t for t in s if t.side == "SELL"]
        print("\n-- 7. Long vs short balance --------------------------------------------")
        report("BUY  (fade bearish group)", longs)
        report("SELL (fade bullish group)", shorts)
        ex = {}
        for t in s:
            ex[t.reason] = ex.get(t.reason, 0) + 1
        print(f"\n  exits: {ex}")

        print("\n-- 9. INVALIDATION: candle breaks back through 0.0 => setup void ---")
        for tag, kw in (
            ("no invalidation (SL only)", dict(invalidate=False)),
            ("inval on CLOSE, grace 0", dict(invalidate=True, inv_close=True, grace_bars=0)),
            ("inval on WICK,  grace 0", dict(invalidate=True, inv_close=False, grace_bars=0)),
            ("inval on CLOSE, grace 1", dict(invalidate=True, inv_close=True, grace_bars=1)),
            ("inval on CLOSE, grace 2", dict(invalidate=True, inv_close=True, grace_bars=2)),
            ("inval on CLOSE, grace 3", dict(invalidate=True, inv_close=True, grace_bars=3)),
            ("inval on WICK,  grace 2", dict(invalidate=True, inv_close=False, grace_bars=2)),
        ):
            r = run(d, ha_o, ha_h, ha_l, ha_c, tp_level=100.0, sequential=True, **{**K, **kw})
            res = report(tag, r)
            if res:
                ex = {}
                for t in r:
                    ex[t.reason] = ex.get(t.reason, 0) + 1
                print(f"      exits {ex}")

        print("\n-- 10. SL buffer x invalidation (sequential, TP 100) -------------------")
        for b in (50, 100, 200, 400):
            report(f"buf {b} + inval", run(d, ha_o, ha_h, ha_l, ha_c, tp_level=100.0,
                                           sequential=True, **{**K, "sl_buf": b}))
        print("\n-- 11. inval + slippage stress (buf 200, grace 2) -----------------------")
        for s in (0, 50, 100, 200, 400):
            report(f"slip {s}pt", run(d, ha_o, ha_h, ha_l, ha_c, tp_level=100.0, sequential=True,
                                      **{**K, "sl_buf": 200, "grace_bars": 2, "slip_pts": s}))

        print("\n-- 12. walk-forward with the FULL rule set (buf 200, grace 2) -----------")
        h2 = nb // 2
        for tag, s, e in (("H1 (older)", 250, h2), ("H2 (newer)", h2, nb - 1), ("FULL", 250, nb - 1)):
            report(tag, run(d, ha_o, ha_h, ha_l, ha_c, tp_level=100.0, sequential=True,
                            start=s, end=e, **{**K, "sl_buf": 200, "grace_bars": 2}))

        print("\n-- 13. STEP 2: TREND filter (align = trade WITH the trend) -------------")
        import MetaTrader5 as _m
        m5t = np.asarray(d["time"], dtype=np.int64)
        tr15, trh1 = htf_trend(m5t, _m.TIMEFRAME_M15), htf_trend(m5t, _m.TIMEFRAME_H1)
        n15 = len(tr15[tr15 > 0]) + len(tr15[tr15 < 0])
        print(f"      (trend known on {n15}/{nb} bars; M15 up={int((tr15>0).sum())} "
              f"down={int((tr15<0).sum())})")
        base_kw = {**K, "sl_buf": 200, "grace_bars": 2}
        for tag, t_arr, mode in (
            ("NO trend filter (baseline)", None, "both"),
            ("M15 trend — align", tr15, "align"),
            ("M15 trend — counter", tr15, "counter"),
            ("H1  trend — align", trh1, "align"),
            ("H1  trend — counter", trh1, "counter"),
        ):
            r = run(d, ha_o, ha_h, ha_l, ha_c, tp_level=100.0, sequential=True,
                    trend=t_arr, trend_mode=mode, **base_kw)
            res = report(tag, r)
            if res:
                d_ = nb * 5 / 60 / 24
                print(f"      -> {res['n']/d_:.1f} trade/hari, "
                      f"{res['exp']*res['n']/d_:,.0f} pt/hari")

        print("\n-- 15. KITARAN 161.8: cycle stays live until 161.8 or a 0.0 break ---")
        for tag, kw in (("no cycle lock", dict(cycle_lock=False)),
                        ("cycle lock (161.8)", dict(cycle_lock=True))):
            r = run(d, ha_o, ha_h, ha_l, ha_c, tp_level=100.0, sequential=True,
                    trend=tr15, trend_mode="align", **{**base_kw, **kw})
            res = report(tag, r)
            if res:
                d_ = nb * 5 / 60 / 24
                print(f"      -> {res['n']/d_:.1f} trade/hari, {res['exp']*res['n']/d_:,.0f} pt/hari")

        print("\n-- 16. IMPROVEMENT: tighten entry zone (TP 100, M15 align) -------------")
        for z in (5.0, 11.8, 17.7, 23.6, 38.2):
            report(f"zone 0-{z}%", run(d, ha_o, ha_h, ha_l, ha_c, tp_level=100.0,
                                       sequential=True, trend=tr15, trend_mode="align",
                                       **{**base_kw, "zone": z}))

        print("\n-- 18. GAP ENTRY: allow up to 61.8 when the candle gaps away -----------")
        for tag, kw in (
            ("zone 23.6 (no gap entry)", dict(zone=23.6)),
            ("zone 23.6, gap to 38.2", dict(zone=23.6, gap_zone=38.2)),
            ("zone 23.6, gap to 50.0", dict(zone=23.6, gap_zone=50.0)),
            ("zone 23.6, gap to 61.8", dict(zone=23.6, gap_zone=61.8)),
            ("zone 11.8, gap to 61.8", dict(zone=11.8, gap_zone=61.8)),
        ):
            r = run(d, ha_o, ha_h, ha_l, ha_c, sequential=True, trend=tr15,
                    trend_mode="align", **{**base_kw, **kw})
            res = report(tag, r)
            if res:
                d_ = nb * 5 / 60 / 24
                print(f"      -> {res['n']/d_:.1f} trade/hari, R/hari={res['er']*res['n']/d_:+.2f}"
                      f"  avg risk {res['risk']:.0f}pt")

        print("\n-- 19. FILTER: only fade legs stretched from EMA20 (x ATR) --------------")
        for k in (0.0, 0.5, 1.0, 1.5, 2.0):
            r = run(d, ha_o, ha_h, ha_l, ha_c, sequential=True, trend=tr15,
                    trend_mode="align", min_ext_atr=k, **base_kw)
            res = report(f"ext >= {k}x ATR", r)
            if res:
                d_ = nb * 5 / 60 / 24
                print(f"      -> R/hari={res['er']*res['n']/d_:+.2f}")

        print("\n-- 20. FILTER: RSI must be extreme at entry ----------------------------")
        for g in (0.0, 25.0, 30.0, 35.0, 40.0, 45.0):
            r = run(d, ha_o, ha_h, ha_l, ha_c, sequential=True, trend=tr15,
                    trend_mode="align", rsi_gate=g, **base_kw)
            res = report(f"RSI needs >= {100-g:.0f} (sell) / <= {g:.0f} (buy)", r)
            if res:
                d_ = nb * 5 / 60 / 24
                print(f"      -> R/hari={res['er']*res['n']/d_:+.2f}")

        print("\n-- 22. TEACHER'S RULE: a 1-candle group is valid once the prior ---------")
        print("       cycle is complete.  Sweep the minimum group length.")
        for lo in (1, 2, 3, 4, 5, 6):
            r = run(d, ha_o, ha_h, ha_l, ha_c, sequential=True, trend=tr15,
                    trend_mode="align", cycle_lock=True,
                    **{**base_kw, "lo_len": lo, "hi_len": 12})
            res = report("min group %d candle%s" % (lo, "" if lo == 1 else "s"), r)
            if res:
                dd_ = nb * 5 / 60 / 24
                print(f"      -> {res['n']/dd_:.1f} trade/hari  R/hari={res['er']*res['n']/dd_:+.2f}")

        print("\n-- 23. TEACHER'S ENTRY label: re-entry at 38.2 -------------------------")
        for re_ in (0.0, 38.2):
            for lo in (1, 3):
                r = run(d, ha_o, ha_h, ha_l, ha_c, sequential=True, trend=tr15,
                        trend_mode="align", cycle_lock=True, reentry=re_,
                        **{**base_kw, "lo_len": lo, "hi_len": 12})
                tag = ("re-entry 38.2" if re_ else "no re-entry") + f", min {lo}"
                res = report(tag, r)
                if res:
                    dd_ = nb * 5 / 60 / 24
                    print(f"      -> R/hari={res['er']*res['n']/dd_:+.2f}  "
                          f"({sum(1 for t in r if t.reason.startswith('RE-'))} re-entries)")
    print("=" * 108)
    ai = mt5_account()
    print(f"  (demo account untouched - backtest only, balance {ai:,.2f})")
    import MetaTrader5 as m
    m.shutdown()
    return 0


def mt5_account() -> float:
    import MetaTrader5 as m
    return m.account_info().balance


if __name__ == "__main__":
    raise SystemExit(main())
