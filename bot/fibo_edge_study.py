"""Does the market reach 161.8 - and where should we actually enter?

The teacher's claim (2026-09-16):
    "the fibo is only our TP indicator. Draw it on a bullish/bearish candle
     group and the market mostly DOES reach 161.8. My problem is finding a
     place to enter, because of time."

So the question splits in two:
  1. HIT RATE - how often does price reach 161.8 before closing back out
     through 0.0?  (his thesis, measured)
  2. EXPECTANCY - but a high hit rate is worthless if the stop is wider than
     the target.  So each bucket also reports the trade in R, using exactly the
     live scalper's rules: enter the zone, stop 200 pt past 0.0, target 100.0.

Filters worth having are the ones that lift R, not the ones that lift the hit
rate.  This script reports both so they can be told apart.

    python fibo_edge_study.py
"""
from __future__ import annotations

import datetime as dt
import sys

import numpy as np

import fiboscalper as S
import fibo_ha_backtest as F
from mt5lib import connect, ema, atr, adx as mt5lib_adx, rsi as mt5lib_rsi

PT = 0.01
LOOKFORWARD = 200          # bars a cycle may run
MIN_GROUP, MAX_GROUP = 1, 60   # was 1,12 - and that silent cap made this whole
                               # study blind to long groups. It reported "longest
                               # group = 12" while the live bot was trading a
                               # 16-candle one, which sent me chasing a bug that
                               # did not exist. fibo_ha_backtest.groups() has no
                               # such cap; this is the tool that lied.
ZONE, WIN, SL_PTS, TP_LEVEL, HOLD, SPREAD = 23.6, 3, 200.0, 100.0, LOOKFORWARD, 7.0


# ------------------------------------------------------------------ helpers
def groups(ha_o, ha_h, ha_l, ha_c):
    """Every ended HA run of one colour, with the fibo anchors applied.

    Mirrors the EA: 0.0 on the group END extreme, 100.0 on the START extreme.
    Bars are OLDEST FIRST, so walking back in time means a LOWER index: the
    group end is the higher index, the start the lower one.
    """
    up = (ha_c >= ha_o).astype(int)
    n = len(up)
    out = []
    j = n - 2                                # newest bar that has actually closed
    while j > LOOKFORWARD:
        b = up[j]
        s = j
        while s > 0 and up[s - 1] == b and (j - s + 1) <= MAX_GROUP + 2:
            s -= 1                           # step OLDER
        run_len = j - s + 1
        if MIN_GROUP <= run_len <= MAX_GROUP and j + 1 < n and up[j + 1] != b:
            seg = slice(s, j + 1)
            ghi, glo = ha_h[seg].max(), ha_l[seg].min()
            out.append(dict(i_end=j, i_start=s, bull=bool(b), len=run_len,
                            p0=ghi if b else glo, p100=glo if b else ghi))
        j = s - 1
    return out


def reached_1618(g, ha_h, ha_l, ha_c):
    """(reached, mfe_fib, bars). We FADE the group: bull -> SELL."""
    p0, p100 = g["p0"], g["p100"]
    leg = abs(p100 - p0)
    if leg < 0.05:
        return None
    sell = g["bull"]
    lv = lambda L: p0 + (p100 - p0) * L / 100.0
    t161 = lv(161.8)
    end = min(g["i_end"] + LOOKFORWARD, len(ha_c) - 1)
    mfe = 0.0
    for k in range(g["i_end"], end + 1):
        if sell:
            mfe = max(mfe, (p0 - ha_l[k]) / leg)
            if ha_l[k] <= t161:
                return True, mfe, k - g["i_end"]
            if k > g["i_end"] and ha_c[k] > p0:
                return False, mfe, k - g["i_end"]
        else:
            mfe = max(mfe, (ha_h[k] - p0) / leg)
            if ha_h[k] >= t161:
                return True, mfe, k - g["i_end"]
            if k > g["i_end"] and ha_c[k] < p0:
                return False, mfe, k - g["i_end"]
    return False, mfe, end - g["i_end"]


def trade_r(g, ha_h, ha_l, ha_c):
    """The live scalper's trade in R. None when price never re-enters the zone."""
    p0, p100 = g["p0"], g["p100"]
    leg = abs(p100 - p0)
    if leg < 0.05:
        return None
    sell = g["bull"]
    lv = lambda L: p0 + (p100 - p0) * L / 100.0
    sl = p0 + SL_PTS * PT if sell else p0 - SL_PTS * PT
    a, b = sorted((lv(0.0), lv(ZONE)))

    # The entry must come from a bar AFTER the group has closed: we only know
    # the leg once the group ends, so a fill inside the group bar would be a
    # price we could never have acted on. Looking at i_end itself is what made
    # one-candle groups look like 98% winners.
    entry = i_entry = None
    for k in range(g["i_end"] + 1, min(g["i_end"] + WIN + 1, len(ha_c))):
        if a <= ha_h[k] and ha_l[k] <= b:            # traded inside the zone
            entry = min(max(ha_c[k], a), b)          # a market fill lands in the zone
            i_entry = k
            break
    if entry is None:
        return None
    tp = lv(TP_LEVEL)
    risk = abs(entry - sl) / PT
    if risk <= 0:
        return None
    cost = SPREAD / risk
    # Start the exit walk on the bar AFTER entry. A one-candle group has p100 as
    # that very candle's far extreme, so checking the entry bar books an instant
    # "target hit" and every trade wins - which is how this was caught.
    for k in range(i_entry + 1, min(i_entry + HOLD, len(ha_c))):
        if sell:
            if ha_l[k] <= tp:
                return (entry - tp) / PT / risk - cost
            if ha_h[k] >= sl:
                return -(sl - entry) / PT / risk - cost
        else:
            if ha_h[k] >= tp:
                return (tp - entry) / PT / risk - cost
            if ha_l[k] <= sl:
                return -(entry - sl) / PT / risk - cost
    k = min(i_entry + HOLD, len(ha_c) - 1)
    mark = (entry - ha_c[k]) if sell else (ha_c[k] - entry)
    return mark / PT / risk - cost


def row(tag, rs, width=34):
    """One bucket: hit rate AND the money, so they can be told apart."""
    if not rs:
        print("  %-*s   (tiada)" % (width, tag))
        return
    hit = np.mean([r["hit"] for r in rs])
    R = np.array([r["r"] for r in rs if r["r"] is not None])
    traded = len(R)
    body = "n=%4d  161.8=%5.1f%%  MFE=%.2f" % (len(rs), 100 * hit,
                                               np.median([r["mfe"] for r in rs]))
    if traded:
        body += "  | tradeable=%4d  win=%4.1f%%  R/hari=%+.3f  expR=%+.3f" % (
            traded, 100 * (R > 0).mean(), R.sum() / DAYS, R.mean())
    else:
        body += "  | tiada entry"
    print("  %-*s %s" % (width, tag, body))


DAYS = 1.0


def main() -> int:
    global DAYS
    connect()
    d = S.load(S.M5, 20000)
    ha_o, ha_h, ha_l, ha_c = F.heikin_ashi(d)
    t5 = np.asarray(d["time"], dtype=np.int64)
    DAYS = len(ha_c) * 5 / 60 / 24
    adx, _, _ = mt5lib_adx(d["high"], d["low"], d["close"], 14)
    adx = np.nan_to_num(adx, nan=0.0)
    rsi = mt5lib_rsi(d["close"], 14)
    a14 = atr(d["high"], d["low"], d["close"], 14)
    e20 = ema(d["close"], 20)
    hours = np.array([dt.datetime.utcfromtimestamp(int(x)).hour for x in t5])

    rows = []
    for g in groups(ha_o, ha_h, ha_l, ha_c):
        o = reached_1618(g, ha_h, ha_l, ha_c)
        if o is None:
            continue
        hit, mfe, bars = o
        k = g["i_end"]
        leg = abs(g["p100"] - g["p0"]) / PT
        rows.append(dict(hit=hit, mfe=mfe, bars=bars, r=trade_r(g, ha_h, ha_l, ha_c),
                         len=g["len"], dir="SELL" if g["bull"] else "BUY",
                         leg=leg, ext=abs(g["p0"] - e20[k]) / max(a14[k], 1e-9),
                         adx=adx[k], rsi=rsi[k], hour=hours[k]))

    print("=" * 112)
    print("TESIS CIKGU: market cecah 161.8  |  %d kumpulan HA, tiada filter, %.0f hari"
          % (len(rows), DAYS))
    print("  '161.8' = kadar harga sampai sasaran mendahului 0.0 ditembusi (close)")
    print("  'expR'  = untung purata setiap trade dalam unit risiko (SL 200pt lepas 0.0)")
    print("=" * 112)

    print("\n[1] KADAR ASAS")
    row("SEMUA kumpulan", rows)

    print("\n[2] PANJANG KUMPULAN - faktor paling kuat")
    for L in range(1, 13):
        row("%2d candle" % L, [r for r in rows if r["len"] == L])
    row("  pendek (1-3)", [r for r in rows if r["len"] <= 3])
    row("  panjang (4-12)", [r for r in rows if r["len"] >= 4])

    print("\n[3] ARAH")
    for s in ("SELL", "BUY"):
        row(s, [r for r in rows if r["dir"] == s])

    print("\n[4] SAIZ LEG (161.8 dari leg kecil = gerak kecil)")
    for lo, hi in ((0, 200), (200, 400), (400, 800), (800, 1e9)):
        row("%4d-%s pt" % (lo, "" if hi > 1e8 else int(hi)),
            [r for r in rows if lo <= r["leg"] < hi])

    print("\n[5] KEREGANGAN 0.0 dari EMA20 - perhatian: scalper sekarang wajib >=1 ATR")
    for lo, hi in ((0, 1), (1, 2), (2, 3), (3, 99)):
        row("%.0f-%.0f ATR" % (lo, hi), [r for r in rows if lo <= r["ext"] < hi])

    print("\n[6] ADX / RSI / JAM")
    for lo, hi in ((0, 20), (20, 25), (25, 35), (35, 999)):
        row("ADX %d-%s" % (lo, "" if hi > 100 else hi),
            [r for r in rows if lo <= r["adx"] < hi])
    for lo, hi in ((0, 30), (30, 45), (45, 55), (55, 70), (70, 101)):
        row("RSI %d-%s" % (lo, "" if hi > 100 else hi),
            [r for r in rows if lo <= r["rsi"] < hi])
    for h in (0, 4, 8, 12, 16, 20):
        row("jam %02d-%02d" % (h, h + 3), [r for r in rows if h <= r["hour"] < h + 4])

    print("\n[7] PENAPIS: sekarang lawan yang data cadang")
    row("SEKARANG  len1-12, ext>=1.0ATR",
        [r for r in rows if r["len"] <= 12 and r["ext"] >= 1.0])
    row("data:     len1-3,  tiada had ext",
        [r for r in rows if r["len"] <= 3])
    row("data:     len1-3,  ext<1.0ATR", [r for r in rows if r["len"] <= 3 and r["ext"] < 1.0])
    row("data:     len<=2", [r for r in rows if r["len"] <= 2])
    row("data:     len<=2,  leg<800pt",
        [r for r in rows if r["len"] <= 2 and r["leg"] < 800])
    print("=" * 112)
    return 0


if __name__ == "__main__":
    sys.exit(main())
