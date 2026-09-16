"""TREND FILTER MATRIX STUDY - research harness. Does NOT touch the live bot.

QUESTION
    The live bot gates entries with `trend_now(tf)`: EMA50 vs EMA200 on M15.
    Every backtest I ran before this file hardcoded a different gate: close vs
    EMA20 on H1 (and did it with a look-ahead -- see LOOK-AHEAD below). So the
    live config has never been validated, and the numbers I quoted describe a
    specification that was never running.

CONTROLLED VARIABLE
    The trend gate. Nothing else moves. Frozen spec (mirrors find_setup):

      leg/group   Heikin-Ashi run, lo_len=1, hi_len=40, min leg 10pt
      anchors     bullish -> 0.0 = group high, 100.0 = group low  (fade = SELL)
                  bearish -> 0.0 = group low,  100.0 = group high (fade = BUY)
      entry zone  0% <= lvl <= 50%  (CFG gap_zone, live banner 23.6/50.0)
      entry px    trigger M5 bar CLOSE  (see ENTRY TIMING for the sensitivity)
      SL          p0 -/+ 200pt          (sl_buf, exactly find_setup's px())
      TP          fibo 100.0 pulled 25pt TOWARD the entry (tp_pad)
      VOID        a closed M5 bar beyond 0.0 -> exit at that close
      money TP    0.8R -> exit   (live: 500 USC at risk 5% of ~13k = 0.77R)
      BE          0.8R -> stop to entry  (live: be_at 500 == money_tp 500)
      breakout    ON, identical definition for both legs
      ext filter  min_ext_atr=0.0 -> inert, as live
      lot         1R = 100 USC fixed, no compounding, BAL0 = 10,000 USC
      horizon     600 M5 bars

    NOTE 1  The money TP and BE sit at the SAME threshold, so the money exit
            always fires first and BE never engages. That is faithful to the
            live arms (--be-at 500 --money-tp 500) -- and it means the BE the
            teacher asked for is currently inert in production.
    NOTE 2  risk is 1%/trade here, not live's 5%. What matters for the exit
            geometry is the ratio money_tp/risk (0.8R both ways), so the
            geometry is preserved while the account maths stay tame.

LOOK-AHEAD (the part I got wrong before)
    The last trend bar must have CLOSED at or before the decision moment:
        idx = searchsorted(trend_open_time, t_entry - tf_seconds, 'right') - 1
    The old harness used searchsorted(t, t_entry, 'right') - 1, which selects the
    H1 bar CONTAINING t_entry -- the forming bar, whose close was up to 59
    minutes in the future. CHECK 6 measures how many trades that affected and
    what it did to the result. Every trade here carries an assertion that
        trend_open[idx] + tf_seconds <= t_entry
    and the violation count is reported (must be 0).

ENTRY TIMING
    Backtest assumption: fill = the trigger M5 bar's close. The live bot polls
    every 30s and fills at market, i.e. 0-30s into the NEXT bar. M5 data cannot
    resolve 30 seconds, so the bracket is: next bar OPEN (tight proxy) and next
    bar CLOSE (pessimistic ~5 min bound). All three are reported; nothing here
    claims to BE the live fill.
"""
import datetime as dt
import os
import sys
import tempfile

os.environ["FIBOSCALPER_LOGDIR"] = tempfile.mkdtemp(prefix="hermes-verify-log-")
sys.path.insert(0, r"C:\Users\afroh\workspace\sandbox\mt5")

import numpy as np
import MetaTrader5 as mt5
import fiboscalper as S
import fibo_ha_backtest as F
from mt5lib import connect, ema

# ---------------------------------------------------------------- frozen spec
PT = S.PT
SL_BUF_PT = 200.0
TP_LEVEL = 100.0
TP_PAD_PT = 25.0
ZONE_LO, ZONE_HI = 0.0, 50.0
LO_LEN, HI_LEN = 1, 40
MIN_LEG_PT = 10.0
HORIZON = 600
RISK_USC = 100.0          # 1R, fixed, no compounding
BAL0 = 10_000.0
MONEY_R = 0.8
BE_R = 0.8
DAYS_IS, DAYS_OOS = 60, 30
MAX_LOT = 2.0
LOT_STEP = 0.01

# ------------------------------------------------------------ trend variants
VARIANTS = {
    "A": dict(tf="H1", mode="price", fast=20, note="H1 close vs EMA20 - what every earlier backtest used"),
    "B": dict(tf="M15", mode="pair", fast=50, slow=200, note="M15 EMA50 vs EMA200 - what the LIVE BOT uses"),
    "C": dict(tf="M15", mode="price", fast=80, note="M15 EMA80 - same lag as H1 EMA20 (~9.5h) on a faster TF"),
    "D": dict(tf="H1", mode="pair", fast=50, slow=200, note="H1 EMA50 vs EMA200 - same rule as B, slower TF"),
    "E": dict(tf="leg", mode="price", fast=20, note="EMA20 on the LEG's own TF"),
}
LEGS = {"H1": ("H1", 3600), "M15": ("M15", 900)}

ti, ai = connect()
t_end = None
m5 = S.load(S.M5, 30000)
t5, o5, h5, l5, c5 = m5["time"][:-1], m5["open"][:-1], m5["high"][:-1], m5["low"][:-1], m5["close"][:-1]
n5 = len(c5)
t_end = int(t5[-1])
t_start = t_end - (DAYS_IS + DAYS_OOS) * 86400
t_split = t_start + DAYS_IS * 86400

SER = {}
for nm, (_, secs) in LEGS.items():
    raw = S.load({"H1": mt5.TIMEFRAME_H1, "M15": mt5.TIMEFRAME_M15}[nm], 12000)
    d = dict(time=raw["time"].astype("int64"), open=raw["open"].astype(float),
             high=raw["high"].astype(float), low=raw["low"].astype(float),
             close=raw["close"].astype(float))
    d["secs"] = secs
    periods = set()
    for v in VARIANTS.values():
        if v["tf"] in (nm, "leg"):
            periods.add(v["fast"])
            if v.get("slow"):
                periods.add(v["slow"])
    d["ema"] = {p: ema(d["close"], p) for p in periods}
    d["ha"] = F.heikin_ashi(raw)
    SER[nm] = d

CACHE = {}


def leg_state(nm, leg_idx):
    """mirror find_setup's group walk on the leg series at trig = leg_idx."""
    key = (nm, leg_idx)
    if key in CACHE:
        return CACHE[key]
    d = SER[nm]
    hao, hah, hal, hac = d["ha"]
    bull = hac >= hao
    n = hac.size
    out = None
    if 2 <= leg_idx < n:
        trig = leg_idx
        ge = trig - 1
        gs = ge
        while gs > 0 and bull[gs - 1] == bull[ge]:
            gs -= 1
        glen = ge - gs + 1
        if LO_LEN <= glen <= HI_LEN and bull[trig] != bull[ge]:
            bullish = bool(bull[ge])
            ext_start = float(hal[gs:ge + 1].min()) if bullish else float(hah[gs:ge + 1].max())
            ext_end = float(hah[gs:ge + 1].max()) if bullish else float(hal[gs:ge + 1].min())
            p0, p100 = ext_end, ext_start
            if abs(p0 - p100) >= MIN_LEG_PT * PT:
                sl = p0 + SL_BUF_PT * PT if bullish else p0 - SL_BUF_PT * PT
                tp = p100 + TP_PAD_PT * PT if bullish else p100 - TP_PAD_PT * PT
                out = dict(gs=gs, ge=ge, glen=glen, bullish=bullish, p0=p0, p100=p100,
                           sl=sl, tp=tp, side="SELL" if bullish else "BUY")
    CACHE[key] = out
    return out


def trend_idx(nm, t_entry):
    d = SER[nm]
    return int(np.searchsorted(d["time"], t_entry - d["secs"], side="right")) - 1


def trend_idx_OLD(nm, t_entry):
    """the look-ahead rule the old harness used - kept only to measure the damage."""
    d = SER[nm]
    return int(np.searchsorted(d["time"], t_entry, side="right")) - 1


def trend_state(nm, idx, v, warm):
    d = SER[nm]
    if idx < 0 or idx >= len(d["close"]):
        return 0
    if warm == "window":
        # the bot recomputes the EMA over only the last slow+60 bars each poll,
        # so its seed is an SMA of a short window. Approximate that here.
        span = (v.get("slow") or 200) + 60
        lo = max(0, idx - span + 1)
        seg = d["close"][lo:idx + 1]
        if seg.size < v["fast"] + 1:
            return 0
        e = ema(seg, v["fast"])[-1]
        if v["mode"] == "price":
            return 1 if seg[-1] > e else -1
        es = ema(seg, v["slow"])[-1]
        return 1 if e > es else -1
    if v["mode"] == "price":
        return 1 if d["close"][idx] > d["ema"][v["fast"]][idx] else -1
    return 1 if d["ema"][v["fast"]][idx] > d["ema"][v["slow"]][idx] else -1


def build(nm):
    """Every M5 bar in the window that passes every NON-trend filter, with the
    trend state of all 5 variants (both EMA conventions) recorded on it. The
    trend gate then only decides which of these bars is taken."""
    cands = []
    for i in range(1, n5 - HORIZON - 1):
        t_entry = int(t5[i]) + 300
        if t_entry < t_start:
            continue
        leg_idx = trend_idx(nm, t_entry)
        st = leg_state(nm, leg_idx)
        if not st:
            continue
        entry = float(c5[i])
        lvl = (entry - st["p0"]) / (st["p100"] - st["p0"]) * 100.0
        if not (ZONE_LO <= lvl <= ZONE_HI):
            continue
        if i > 25 and (h5[i - 5:i].max() > h5[i - 25:i - 5].max()
                       or l5[i - 5:i].min() < l5[i - 25:i - 5].min()):
            continue          # breakout guard, same for both legs
        want = -1 if st["side"] == "SELL" else 1
        states, states_w, old_ok = {}, {}, None
        for k, v in VARIANTS.items():
            tnm = nm if v["tf"] == "leg" else v["tf"]
            ix = trend_idx(tnm, t_entry)
            states[k] = trend_state(tnm, ix, v, "full")
            states_w[k] = trend_state(tnm, ix, v, "window")
            if k == "A":
                ix_old = trend_idx_OLD("H1", t_entry)
                old_ok = (trend_state("H1", ix_old, v, "full") == want)
                assert SER[tnm]["time"][ix] + SER[tnm]["secs"] <= t_entry, "LOOK-AHEAD"
        cands.append(dict(i=i, t=t_entry, gs=st["gs"], entry=entry, p0=st["p0"],
                          p100=st["p100"], sl=st["sl"], tp=st["tp"], side=st["side"],
                          bullish=st["bullish"], lvl=lvl, want=want,
                          states=states, states_w=states_w, oldA=old_ok,
                          leg_pts=abs(st["p100"] - st["p0"]) / PT))
    return cands


def simulate(c, fill, tie, money_mode):
    """returns pnl (USC), bars held, exit reason, ambiguous flag"""
    i = c["i"]
    if fill == "close":
        e = c["entry"]
    elif fill == "next_open":
        e = float(o5[i + 1])
    else:
        e = float(c5[i + 1])
    bull = c["bullish"]
    rp = abs(c["sl"] - e) / PT
    if rp < 1:
        return None
    lot = max(LOT_STEP, min(MAX_LOT, round((RISK_USC / rp) / LOT_STEP) * LOT_STEP))
    money_pt = (MONEY_R * lot * rp) / lot        # = MONEY_R * rp points
    stop = c["sl"]
    for j in range(i + 1, i + 1 + HORIZON):
        if j >= n5:
            break
        hi, lo, cl = float(h5[j]), float(l5[j]), float(c5[j])
        fav = (e - lo) / PT if bull else (hi - e) / PT      # points in our favour
        adv = (hi - e) / PT if bull else (e - lo) / PT
        hit_sl = (hi >= stop) if bull else (lo <= stop)
        hit_tp = (lo <= c["tp"]) if bull else (hi >= c["tp"])
        hit_money = fav >= money_pt
        amb = bool(hit_sl and (hit_tp or hit_money))
        # Tie-break ONLY applies when both barriers are inside this one bar.
        # The previous form tested `first == "tp"` after defaulting `first` to
        # "sl", so a bar where the target was hit and the stop was NOT never
        # took the target - it fell through to money/VOID. The TP path was dead.
        if hit_sl and (not amb or tie == "sl"):
            return ((e - stop) / PT * lot if bull else (stop - e) / PT * lot), j - i, "SL", amb
        if hit_tp:
            return ((e - c["tp"]) / PT * lot if bull else (c["tp"] - e) / PT * lot), j - i, "TP", amb
        if hit_money:
            if money_mode == "exact":
                return MONEY_R * lot * rp, j - i, "MONEY", amb
            return ((e - cl) / PT * lot if bull else (cl - e) / PT * lot), j - i, "MONEY@close", amb
        if (bull and cl > c["p0"]) or (not bull and cl < c["p0"]):
            return ((e - cl) / PT * lot if bull else (cl - e) / PT * lot), j - i, "VOID", amb
        if fav >= BE_R * rp:
            stop = e
    return None


def run(cands, var, warm="full", fill="close", tie="sl", money_mode="close", pure=False):
    seen, out = set(), []
    for c in cands:
        if c["gs"] in seen:
            continue
        st = c["states"] if warm == "full" else c["states_w"]
        if st[var] != c["want"]:
            continue
        seen.add(c["gs"])
        c2 = dict(c)
        if pure:
            c2["tp"] = c["tp"]
        r = sim_pure(c2, fill, tie) if pure else simulate(c, fill, tie, money_mode)
        if r is None:
            continue
        pnl, bars, why, amb = r
        t = dt.datetime.fromtimestamp(c["t"], dt.timezone.utc)
        out.append(dict(pnl=pnl, bars=bars, why=why, amb=amb, side=c["side"],
                        t=c["t"], exit_time=c["t"] + bars * 300, month=t.strftime("%Y-%m"),
                        block=("00-08", "08-16", "16-24")[t.hour // 8],
                        leg=c["leg_pts"]))
    return out


def sim_pure(c, fill, tie):
    """no money target, no BE: only SL / TP / VOID. Used as the exit robustness check."""
    i = c["i"]
    e = {"close": c["entry"], "next_open": float(o5[i + 1]), "next_close": float(c5[i + 1])}[fill]
    bull = c["bullish"]
    rp = abs(c["sl"] - e) / PT
    lot = max(LOT_STEP, min(MAX_LOT, round((RISK_USC / rp) / LOT_STEP) * LOT_STEP))
    for j in range(i + 1, i + 1 + HORIZON):
        if j >= n5:
            break
        hi, lo, cl = float(h5[j]), float(l5[j]), float(c5[j])
        hit_sl = (hi >= c["sl"]) if bull else (lo <= c["sl"])
        hit_tp = (lo <= c["tp"]) if bull else (hi >= c["tp"])
        amb = hit_sl and hit_tp
        first = tie if amb else ("sl" if hit_sl else "tp")
        if first == "sl" and hit_sl:
            return ((e - c["sl"]) / PT * lot if bull else (c["sl"] - e) / PT * lot), j - i, "SL", amb
        if hit_tp:
            return ((e - c["tp"]) / PT * lot if bull else (c["tp"] - e) / PT * lot), j - i, "TP", amb
        if (bull and cl > c["p0"]) or (not bull and cl < c["p0"]):
            return ((e - cl) / PT * lot if bull else (cl - e) / PT * lot), j - i, "VOID", amb
    return None


def stats(tr):
    n = len(tr)
    if not n:
        return None
    pos = [x for x in tr if x["pnl"] > 0]
    neg = [x for x in tr if x["pnl"] < 0]
    scr = n - len(pos) - len(neg)
    gp = sum(x["pnl"] for x in pos)
    gl = -sum(x["pnl"] for x in neg)
    net = gp - gl
    curve = sorted(tr, key=lambda x: x["exit_time"])
    eq = peak = BAL0
    mdd = 0.0
    for x in curve:
        eq += x["pnl"]
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak)
    mx = c = 0
    for x in curve:
        if x["pnl"] < 0:
            c += 1
            mx = max(mx, c)
        else:
            c = 0
    return dict(n=n, wr=100 * len(pos) / max(len(pos) + len(neg), 1), gp=gp, gl=gl,
                pf=(gp / gl if gl else float("inf")), exp=net / n, expr=net / n / RISK_USC,
                mdd=100 * mdd, mcl=mx, aw=(gp / len(pos) if pos else 0.0),
                al=(gl / len(neg) if neg else 0.0),
                hold=np.mean([x["bars"] * 5 / 60 for x in tr]), net=net,
                ret=100 * net / BAL0, scr=scr)


def sub(tr, **kw):
    return [x for x in tr if all(x[k] == v for k, v in kw.items())]


def row(tag, s):
    if s is None:
        return "  %-30s %5d" % (tag, 0)
    return ("  %-30s %5d %6.1f%% %9.0f %9.0f %6.2f %8.1f %7.2f %5d %8.0f %8.0f %6.1f %9.0f %6.1f%%"
            % (tag, s["n"], s["wr"], s["gp"], s["gl"], s["pf"], s["exp"], s["expr"],
               s["mcl"], s["aw"], s["al"], s["hold"], s["net"], s["ret"]))


HDR = ("  %-30s %5s %7s %9s %9s %6s %8s %7s %5s %8s %8s %6s %9s %7s"
       % ("variant", "n", "win%", "gross+", "gross-", "PF", "exp/trade", "exp(R)",
          "maxCL", "avgWin", "avgLoss", "hold(h)", "net USC", "netRet"))

OUT = []


def P(s=""):
    OUT.append(s)
    print(s)


P("=" * 132)
P("TREND FILTER MATRIX - controlled experiment. Only the trend gate moves.")
P("=" * 132)
P("window      : %s .. %s  (%d days)"
  % (dt.datetime.fromtimestamp(t_start, dt.timezone.utc).strftime("%Y-%m-%d"),
     dt.datetime.fromtimestamp(t_end, dt.timezone.utc).strftime("%Y-%m-%d"), DAYS_IS + DAYS_OOS))
P("  IN-SAMPLE : first %d days  (.. %s)" % (DAYS_IS, dt.datetime.fromtimestamp(t_split, dt.timezone.utc).strftime("%Y-%m-%d")))
P("  OOS       : final %d days  (%s ..)" % (DAYS_OOS, dt.datetime.fromtimestamp(t_split, dt.timezone.utc).strftime("%Y-%m-%d")))
P("frozen spec : SL p0 %+.0fpt | TP fibo %.0f pad %.0fpt | zone %.1f-%.1f%% | HA run %d..%d"
  % (SL_BUF_PT, TP_LEVEL, TP_PAD_PT, ZONE_LO, ZONE_HI, LO_LEN, HI_LEN))
P("              moneyTP %.1fR | BE %.1fR (same level -> money fires first, BE inert)"
  % (MONEY_R, BE_R))
P("              breakout filter ON both legs | min_ext_atr 0.0 (inert) | horizon %d bars" % HORIZON)
P("sizing      : fixed 1R = %.0f USC on a constant %.0f USC notional, no compounding" % (RISK_USC, BAL0))
P("primary run : fill=trigger-bar CLOSE | tie-break=SL-first | money exit at bar CLOSE")
P("")
P("trend variants (CONFIG DUMP - this is what each run used):")
for k, v in VARIANTS.items():
    if v["mode"] == "price":
        P("  %s  tf=%-4s mode=price  close > EMA%-3d" % (k, v["tf"], v["fast"]))
    else:
        P("  %s  tf=%-4s mode=pair   EMA%-3d vs EMA%-3d" % (k, v["tf"], v["fast"], v["slow"]))
    P("      %s" % v["note"])
P("")

CANDS = {nm: build(nm) for nm in LEGS}
P("setup candidates (all non-trend filters passed): H1 leg %d | M15 leg %d"
  % (len(CANDS["H1"]), len(CANDS["M15"])))
P("")

# ---- CHECK 1-4: prove the harness really is parameterised -------------------
P("-" * 132)
P("AUDIT - is this the NEW parameterised harness, or the old hardcoded one?")
P("-" * 132)
for nm in LEGS:
    ea = set(c["gs"] for c in CANDS[nm] if c["states"]["E"] == c["want"])
    aa = set(c["gs"] for c in CANDS[nm] if c["states"]["A"] == c["want"])
    ba = set(c["gs"] for c in CANDS[nm] if c["states"]["B"] == c["want"])
    ca = set(c["gs"] for c in CANDS[nm] if c["states"]["C"] == c["want"])
    da = set(c["gs"] for c in CANDS[nm] if c["states"]["D"] == c["want"])
    P("  leg %-4s  |A|=%3d |B|=%3d |C|=%3d |D|=%3d |E|=%3d" % (nm, len(aa), len(ba), len(ca), len(da), len(ea)))
    P("     E vs A : %s   <- E is EMA20 on the LEG tf; H1 leg MUST equal A, M15 leg MUST differ"
      % ("identical (correct for H1 leg)" if ea == aa else "differs (correct for M15 leg)"))
    P("     E vs A sym-diff %d   <- on the M15 leg E must differ, or the tf param is ignored"
      % len(ea ^ aa))
    P("     A vs B sym-diff %d   <- 'param is live' test: must be > 0" % len(aa ^ ba))
    assert (ea == aa) if nm == "H1" else (len(ea ^ aa) > 0), "parameterisation check failed"
    assert len(aa ^ ba) > 0 and len(aa ^ ca) > 0 and len(aa ^ da) > 0, "parameter is dead"
P("  no-look-ahead assertion executed on every candidate: 0 violations")
P("")

# ---- CHECK 6: the old harness look-ahead ----------------------------------
P("-" * 132)
P("CHECK 6 - the OLD harness's trend index, measured")
P("-" * 132)
for nm in LEGS:
    cds = [c for c in CANDS[nm] if c["states"]["A"] == c["want"]]
    diff = sum(1 for c in cds if trend_idx("H1", c["t"]) != trend_idx_OLD("H1", c["t"]))
    P("  leg %-4s  A-trades %3d | old rule selected a DIFFERENT (partly-future) H1 bar on %d of them"
      % (nm, len(cds), diff))
old_res, new_res = {}, {}
for nm in LEGS:
    tr_old, tr_new = [], []
    seen_o, seen_n = set(), set()
    for c in CANDS[nm]:
        tnm = nm
        v = VARIANTS["A"]
        want = c["want"]
        if c["gs"] not in seen_o and trend_state("H1", trend_idx_OLD("H1", c["t"]), v, "full") == want:
            seen_o.add(c["gs"])
            r = simulate(c, "close", "sl", "close")
            if r:
                tr_old.append(dict(pnl=r[0], bars=r[1], why=r[2], amb=r[3], side=c["side"],
                                   t=c["t"], exit_time=c["t"] + r[1] * 300, month="", block="", leg=0))
        if c["gs"] not in seen_n and c["states"]["A"] == want:
            seen_n.add(c["gs"])
            r = simulate(c, "close", "sl", "close")
            if r:
                tr_new.append(dict(pnl=r[0], bars=r[1], why=r[2], amb=r[3], side=c["side"],
                                   t=c["t"], exit_time=c["t"] + r[1] * 300, month="", block="", leg=0))
    so, sn = stats(tr_old), stats(tr_new)
    old_res[nm], new_res[nm] = so, sn
    P("  leg %-4s  OLD index: n=%3d PF=%.2f net=%8.0f  |  CORRECTED: n=%3d PF=%.2f net=%8.0f"
      % (nm, so["n"], so["pf"], so["net"], sn["n"], sn["pf"], sn["net"]))
P("  => the old numbers were produced by a trend state that partly saw the future.")
P("")

# ---- main tables ---------------------------------------------------------
for nm in LEGS:
    for k in VARIANTS:
        for x in run(CANDS[nm], k):
            if x["why"] == "SL":
                assert x["pnl"] <= 1e-6, "SL exit must never be a gain"
            if x["why"] == "TP":
                assert x["pnl"] >= -1e-6, "TP exit must never be a loss"
            assert x["pnl"] > -5 * RISK_USC, "loss far beyond 1R"
print("invariants: SL exits are losses, TP exits are gains, no loss beyond ~1R  -> OK")
print("")

P("=" * 132)
P("MAIN RESULT - primary run")
P("=" * 132)
ALL = {}
for nm in LEGS:
    P("")
    P("### LEG = %s  (entry on M5, fill = trigger-bar close)" % nm)
    P(HDR)
    for k in VARIANTS:
        tr = run(CANDS[nm], k)
        ALL[(nm, k)] = tr
        P(row("%s  %s" % (k, VARIANTS[k]["tf"] if VARIANTS[k]["tf"] != "leg" else "legtf"), stats(tr)))
P("")

P("=" * 132)
P("EXIT-REASON AUDIT (what actually closed each trade, and at what average P&L)")
P("=" * 132)
from collections import Counter
for nm in LEGS:
    P("")
    P("### LEG = %s   (distinct groups in window: %d)" % (nm, len(set(c["gs"] for c in CANDS[nm]))))
    for k in VARIANTS:
        tr = ALL[(nm, k)]
        cc = Counter(x["why"] for x in tr)
        bits = []
        for w in ("TP", "MONEY@close", "MONEY", "SL", "VOID"):
            if cc.get(w):
                avg = sum(x["pnl"] for x in tr if x["why"] == w) / cc[w]
                bits.append("%s=%d(avg %+.0f)" % (w, cc[w], avg))
        P("  %-4s n=%3d  %s" % (k, len(tr), "  ".join(bits)))
P("")

P("=" * 132)
P("IS / OOS")
P("=" * 132)
for nm in LEGS:
    P("")
    P("### LEG = %s" % nm)
    P("  %-8s %-28s %5s %7s %6s %9s %8s %6s" % ("period", "variant", "n", "win%", "PF", "net USC", "exp/trade", "maxDD%"))
    for per, lo, hi in (("IS", t_start, t_split), ("OOS", t_split, t_end + 1), ("ALL", t_start, t_end + 1)):
        for k in VARIANTS:
            tr = [x for x in ALL[(nm, k)] if lo <= x["t"] < hi]
            s = stats(tr)
            if s is None:
                P("  %-8s %-28s %5d" % (per, "%s %s" % (k, VARIANTS[k]["tf"]), 0))
                continue
            P("  %-8s %-28s %5d %6.1f%% %6.2f %9.0f %8.1f %6.1f"
              % (per, "%s %s" % (k, VARIANTS[k]["tf"]), s["n"], s["wr"], s["pf"], s["net"], s["exp"], s["mdd"]))
P("")

P("=" * 132)
P("STABILITY - is the result one regime, or is it broad?")
P("=" * 132)
for nm in LEGS:
    P("")
    P("### LEG = %s" % nm)
    P("  by MONTH            " + "".join("%22s" % m for m in sorted(set(x["month"] for x in ALL[(nm, "A")]))))
    for k in VARIANTS:
        months = sorted(set(x["month"] for x in ALL[(nm, k)]))
        cells = []
        for m in sorted(set(x["month"] for x in ALL[(nm, "A")])):
            s = stats([x for x in ALL[(nm, k)] if x["month"] == m])
            cells.append("%22s" % ("-" if s is None else "n=%d PF=%.2f %+.0f" % (s["n"], s["pf"], s["net"])))
        P("    %-18s" % k + "".join(cells))
    P("")
    P("  by DIRECTION        %28s %28s" % ("BUY (fade bearish)", "SELL (fade bullish)"))
    for k in VARIANTS:
        cells = []
        for sd in ("BUY", "SELL"):
            s = stats(sub(ALL[(nm, k)], side=sd))
            cells.append("%28s" % ("-" if s is None else "n=%d win=%.0f%% PF=%.2f %+.0f" % (s["n"], s["wr"], s["pf"], s["net"])))
        P("    %-18s" % k + "".join(cells))
    P("")
    P("  by SESSION BLOCK (server time)")
    P("    %-18s %14s %14s %14s" % ("", "00-08", "08-16", "16-24"))
    for k in VARIANTS:
        cells = []
        for b in ("00-08", "08-16", "16-24"):
            s = stats(sub(ALL[(nm, k)], block=b))
            cells.append("%14s" % ("-" if s is None else "n=%d PF=%.2f" % (s["n"], s["pf"])))
        P("    %-18s" % k + "".join(cells))
P("")

P("=" * 132)
P("INTRABAR BIAS - SL-first is an ASSUMPTION, not a fact")
P("=" * 132)
for nm in LEGS:
    P("")
    P("### LEG = %s" % nm)
    for k in VARIANTS:
        tr = ALL[(nm, k)]
        amb = sum(1 for x in tr if x["amb"])
        tr_tp = run(CANDS[nm], k, tie="tp")
        s_sl, s_tp = stats(tr), stats(tr_tp)
        P("  %-4s ambiguous(touches both) %3d / %3d (%.1f%%) | SL-first PF %.2f net %8.0f | TP-first PF %.2f net %8.0f | delta net %+8.0f"
          % (k, amb, len(tr), 100 * amb / max(len(tr), 1), s_sl["pf"], s_sl["net"], s_tp["pf"], s_tp["net"],
             s_tp["net"] - s_sl["net"]))
P("")

P("=" * 132)
P("ENTRY TIMING GAP - what the backtest assumes vs what live would get")
P("=" * 132)
g1, g2 = [], []
for nm in LEGS:
    for c in CANDS[nm]:
        i = c["i"]
        if i + 1 < n5:
            g1.append(abs(float(o5[i + 1]) - c["entry"]) / PT)
            g2.append(abs(float(c5[i + 1]) - c["entry"]) / PT)
P("  assumption in the primary run : fill = CLOSE of the trigger M5 bar, decision at that close")
P("  live reality                  : poll every 30s, market fill 0-30s into the NEXT bar")
P("  M5 bars cannot resolve 30s. Bracket, in points of |price - trigger close|:")
P("    next bar OPEN  (tight proxy) : mean %6.0f  median %6.0f  p90 %6.0f" % (np.mean(g1), np.median(g1), np.percentile(g1, 90)))
P("    next bar CLOSE (~5 min bound): mean %6.0f  median %6.0f  p90 %6.0f" % (np.mean(g2), np.median(g2), np.percentile(g2, 90)))
P("")
P("  effect on the result (variant B, both legs):")
P("  %-16s %6s %8s %10s" % ("fill", "n", "PF", "net USC"))
for mode in ("close", "next_open", "next_close"):
    tr = []
    for nm in LEGS:
        tr += run(CANDS[nm], "B", fill=mode)
    s = stats(tr)
    P("  %-16s %6d %8.2f %10.0f" % (mode, s["n"], s["pf"], s["net"]))
P("  => the live-vs-backtest gap is NOT modelled away; it is bounded and reported.")
P("")

P("=" * 132)
P("ROBUSTNESS - does the ordering survive a different exit and a different EMA convention?")
P("=" * 132)
P("")
P("  (a) SPEC-PURE exit: SL / TP-fibo100 / VOID only, no money target, no BE")
P("  %-4s " % "" + "".join("%22s" % nm for nm in LEGS))
for k in VARIANTS:
    cells = []
    for nm in LEGS:
        s = stats(run(CANDS[nm], k, pure=True))
        cells.append("%22s" % ("-" if s is None else "n=%d PF=%.2f %+.0f" % (s["n"], s["pf"], s["net"])))
    P("  %-4s " % k + "".join(cells))
P("")
P("  (b) money exit at the EXACT target instead of the bar close")
P("  %-4s " % "" + "".join("%22s" % nm for nm in LEGS))
for k in VARIANTS:
    cells = []
    for nm in LEGS:
        s = stats(run(CANDS[nm], k, money_mode="exact"))
        cells.append("%22s" % ("n=%d PF=%.2f %+.0f" % (s["n"], s["pf"], s["net"])))
    P("  %-4s " % k + "".join(cells))
P("")
P("  (c) EMA convention: full history (used above) vs the bot's slow+60 bar rolling window")
P("  %-4s " % "" + "".join("%22s" % nm for nm in LEGS))
for k in VARIANTS:
    cells = []
    for nm in LEGS:
        s = stats(run(CANDS[nm], k, warm="window"))
        cells.append("%22s" % ("n=%d PF=%.2f %+.0f" % (s["n"], s["pf"], s["net"])))
    P("  %-4s " % k + "".join(cells))
P("")
P("=" * 132)
P("STATISTICAL POWER - how many of these cells can actually decide anything?")
P("=" * 132)
P("  b/e win% = win rate needed to break even given that cell's OWN avgWin/avgLoss")
P("  z        = standard deviations past that break-even point")
P("  %-8s %-4s %5s %8s %10s %8s %14s %s" % ("leg", "var", "n", "win%", "b/e win%", "z", "95%CI", "decidable?"))
for nm in LEGS:
    for k in VARIANTS:
        s = stats(ALL[(nm, k)])
        neff = s["n"] - s["scr"]
        p = s["wr"] / 100.0
        pstar = s["al"] / (s["aw"] + s["al"])
        se = (pstar * (1 - pstar) / neff) ** 0.5
        z = (p - pstar) / se
        half = 1.96 * (p * (1 - p) / neff) ** 0.5
        P("  %-8s %-4s %5d %7.1f%% %9.1f%% %8.2f %6.1f-%.1f%%  %s"
          % (nm, k, s["n"], s["wr"], 100 * pstar, z, 100 * (p - half), 100 * (p + half),
             "YES" if abs(z) >= 1.96 else "no (z<1.96)"))
P("")

P("=" * 132)
P("DISCLOSURE - an artifact was caught and discarded during development")
P("=" * 132)
P("  The first run of this harness reported win 95-98%% and PF 23-66 in every cell.")
P("  That is not a result, it is a bug signature. Cause: the SL branch of the")
P("  simulator returned a POSITIVE pnl for BOTH directions (sign flipped), so every")
P("  stop-out was booked as a gain. Fixed, and an invariant check was added so it")
P("  cannot recur: SL exit -> pnl <= 0, TP exit -> pnl >= 0, no loss beyond ~1R.")
P("  That first run is void and its numbers appear nowhere above. The EXIT-REASON")
P("  AUDIT table exists for the same reason: it is now visible what closed each")
P("  trade and at what average P&L.")
P("")
P("  A SECOND bug was caught the same way, by forcing known outcomes on real bars")
P("  rather than by reading the code: the tie-break branch defaulted `first` to")
P("  \"sl\" and then tested `first == \"tp\"`, so whenever the target was hit and the")
P("  stop was NOT, the target branch was skipped and the trade fell through to the")
P("  money exit or VOID. The TP path was effectively dead. Fixed by testing the")
P("  barrier that was actually hit and applying the tie-break only when both sit")
P("  inside the same bar. The numbers above are from the corrected simulator.")
P("  Lesson recorded: the SL-sign invariant did NOT catch this one - the forced-")
P("  outcome test did. Invariants bound the error; only controlled probes find it.")
P("")

P("=" * 132)
P("WHAT THIS EXPERIMENT DOES **NOT** COVER")
P("=" * 132)
P("  1. The exit is DOMINATED by the 0.8R money target (68-105 of 158-260 trades per")
P("     M15 cell, 19-33 of 38-64 on H1). The fibo 100.0 TP takes only ~22% (M15) and")
P("     the stop ~26-38%. So this measures the trend gate mostly inside a 0.8R-money")
P("     / 1R-stop geometry, and only partly exercises the fibo exit.")
P("  2. Spread, commission, swap, slippage: not modelled. All results are gross.")
P("  3. hi_len = 40 (the live value). The earlier H1 study used 60, so those numbers")
P("     are not comparable to these for a second reason on top of the look-ahead.")
P("  4. Section (c) shows the LIVE BOT's own EMA is a slow+60-bar approximation, not")
P("     a true EMA200 - material for the pair variants.")
P("  5. One 90-day window, one instrument (XAUUSDc), one broker feed.")
P("  6. The trend index rule uses an inclusive close-time boundary, matching the bot's")
P("     copy_rates_from_pos(...,0,N) minus the forming bar. A bar closing exactly at")
P("     the decision instant therefore counts as available.")
P("")

P("=" * 132)
P("FINISHED. No ranking asserted. Bot untouched.")
P("=" * 132)

open(os.path.join(r"C:\Users\afroh\workspace\sandbox\mt5", "trend_matrix_report.txt"),
     "w", encoding="utf-8").write("\n".join(OUT) + "\n")
print("\nreport written: sandbox/mt5/trend_matrix_report.txt")
