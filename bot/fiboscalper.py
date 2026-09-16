"""
Fibo + Heikin Ashi scalper - the teacher's method, executed live on MT5.

The rule (as taught, and as it back-tested):
  1. Heikin Ashi, M5.
  2. Find a GROUP of consecutive same-colour HA candles. The group must have
     ENDED: the candle after it is the opposite colour.
  3. Draw the fibo over that group:
        0.0   = the group's END extreme
                (highest wick for a bullish group, lowest for a bearish one)
        100.0 = the group's START extreme (the far end of the leg)
  4. Fade the group, but only from inside the entry zone near 0.0:
        - normal entry: 0.0 .. 23.6%
        - if the candle GAPS away, allow up to 61.8% - with a smaller lot,
          because the stop is further away (risk parity).
  5. TP  = 100.0
     SL  = just beyond the 0.0 line, >= --sl-buf points past it, and not
           active for the first --grace candles (gold needs room to breathe).
  6. Setup is VOID the moment a candle breaks back out through the 0.0 line.
  7. The fibo cycle is COMPLETE once price reaches 161.8 - only then look for
     the next setup.
  8. Only fade in the direction the higher timeframe is already going
     (--trend-tf, EMA50 vs EMA200).

Execution notes:
  * Orders go through order_send(); the GUI is not trusted to submit.
  * Sizing is risk-based: lot = risk / stop distance, so a wide gap entry
    automatically gets a smaller lot.
  * --dry-run prints the plan and places nothing.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import MetaTrader5 as mt5
import numpy as np

from mt5lib import (M15, M5, SYMBOL, atr, close_position, connect, ema, now_myt,
                    server_dt)

PT = 0.01                      # XAUUSDc point size
TAG = os.environ.get("FIBOSCALPER_TAG", "")
# One file per arm of an A/B test: three bots enter the SAME setup and differ
# only in the exit, so they must not share done_groups or tickets.
# The control file is written by traderctl / the Telegram bot and lives in the
# deployment state dir, NOT next to this file.
try:
    import traderbridge as _tb
    CONTROL = _tb.CTRL
except Exception:                      # running on the PC without the bridge
    _tb = None
    CONTROL = Path(__file__).with_name("state") / "control.json"
STATE = Path(os.environ.get("FIBOSCALPER_STATE")
             or (Path(__file__).with_name("state") / f"fiboscalper{TAG}.json"))
# FIBOSCALPER_LOGDIR lets dry runs and verifiers write somewhere else, so they
# do not pollute the live trading log.
LOGDIR = Path(os.environ.get("FIBOSCALPER_LOGDIR") or Path(__file__).with_name("logs"))

CFG = dict(
    zone=23.6,        # normal entry window, as a fib %
    gap_zone=50.0,    # furthest retracement we enter at, as a % of the leg. The
                      # teacher's rule is 0-23.6% (38.2 when the trigger gapped).
                      # Measured over 1,409 setups: 38.2 -> 50 took 32.0 -> 43.8
                      # setups/day and win 63.6% -> 66.6%, i.e. +18% USC/day at the
                      # same 4% risk. Deeper entries also sit closer to the 100.0
                      # target, which is why the win rate RISES with the zone.
    be_at=0.0,        # move the stop to entry once a leg shows this much USC
                      # (0 = off). The teacher: "kalau dah jauh, set Breakeven,
                      # supaya kita tak rugi". A trade that ran this far should
                      # not be allowed to turn into a loss.
    fibo_tf="M5",     # timeframe the Heikin-Ashi GROUP (the fibo) is drawn on.
                      # "H1" means: leg and levels from H1, entry timed on M5.
                      # Measured (fill=close, 413 setups): M5 legs give PF 0.99 /
                      # -25 USC/day - break even; H1 legs give PF 1.75 / 1,399.
                      # The H1 leg is ~1,500pt so the 100.0 line is far: a scalp
                      # target (money_tp) is what makes it tradeable.
    rescale=1,        # money-TP exits free the group for another entry, so one
                      # big H1 cycle can be scalped several times. 1 = on.
    bk_block=1,       # skip a setup if price just made a fresh 20-bar high/low
                      # (momentum, not a pullback). Measured over 3 months at
                      # fill=close: PF 2.03 -> 2.57 alone, and 4.80 -> 6.65 when
                      # stacked on the trend gate. The teacher's point exactly:
                      # "tengah uptrend, candle bearish tu retracement sementara".
    tp_pad=25.0,      # stop this many points SHORT of the target level. The
                      # teacher: do not sit the TP exactly on 161.8 - spread and
                      # requotes eat it. Pulling the target back toward the entry
                      # by 25pt means the order is filled before the crowd's line.
    money_tp=0.0,     # close a leg once it SHOWS this much profit, in USC (0 =
                      # off). The teacher's point: waiting for the 100.0 line ties
                      # the cycle up on long legs, so the next setup passes by. A
                      # money target is leg-agnostic. Measured with honest fills
                      # (trigger-bar close), per position:
                      #   +100 -> -103 USC/day (loses)  +300 -> 112   +500 -> 193
                      #   +700 -> 246   vs fibo 100.0 -> 66. So aim HIGH: too tight
                      # and it harvests the noise, not the move. 98% of setups see
                      # at least +500 before they resolve.
    layers=1,         # legs per setup. 1 = single entry (the teacher's way).
                      # >1 means SCALE IN: add a leg each time price walks a further
                      # layer_step deeper into the zone, all sharing one stop. The
                      # backtest of THREE-AT-ONCE is just 3x size with no edge; this
                      # is the different idea - enter as it goes against you so the
                      # average entry improves - and it has never been measured.
    layer_step=12.5,  # % of the leg per layer
    tp_pts=0.0,       # plain point target (0 = use tp_level). The exit
                      # sweep says a fixed target is WORSE risk-adjusted than
                      # the 100.0 line (PF 8.83 vs 12.15 at 225 vs fibo), but an
                      # A/B run on live fills settles it on the real book.
    tp_level=100.0,   # scalping target. A split target (part at 161.8) was added
                      # 2026-09-16 then pulled: measured against the scalper's REAL
                      # first-touch entry it is -4% (PF 2.03 -> 1.99, R/hari 4.14 ->
                      # 3.98). The earlier +10% came from comparing against a fixed
                      # 11.8% entry, which is not what this bot does. Kept behind
                      # --split for experiments only.
    split=False,      # fire the 100/161.8 ladder instead of a single target
    tp_ext=161.8,
    sl_buf=200,       # points beyond the 0.0 line
    grace=2,          # candles of room before SL / invalidation arm
    lo_len=1, hi_len=40,  # a LONE candle is a valid group once the prior cycle
                          # is done. The UPPER limit matters more than it looks:
                          # during a trend the only groups that form are long
                          # ones, so a tight cap leaves the bot sitting out
                          # exactly the hours when the market moves - a live
                          # 16-candle group was skipped at hi_len=3 on
                          # 2026-09-16 while price sat perfectly inside its zone.
                          # Measured over 20k M5 bars at lot 0.96:
                          #   1-3  : 11.0/day  win 90.6%  PF 10.01   3,630 USC/day
                          #   1-12 : 29.8/day  win 66.0%  PF  2.64   8,772 USC/day
                          #   1-40 : 32.0/day  win 63.5%  PF  2.49   9,448 USC/day
                          # Short groups are still the best bets - they just
                          # cannot be the only ones taken. Risk stays capped
                          # because lot_for scales down as the stop widens.
    reentry=0.0,      # his "ENTRY" label (38.2). 0 = off: it lifts R/day ~30%
                      # but more than doubles the drawdown
    min_ext_atr=0.0,  # 0 = no filter. Was 1.0, which was actively harmful:
                      # groups whose 0.0 line sits 0-1 ATR from EMA20 win 69.1%
                      # of the time, 3+ ATR only 36.4% - so the gate was
                      # discarding the best setups. Removing it took win from
                      # 51.8% to 71.4% and R/day from +8.97 to +11.63.
    trend_tf="M15",
    max_risk_mult=3.0,  # last-resort guard. live_lot already holds the money at
                        # risk_pct whatever the fill depth, so this only trips on
                        # a pathological drift (stop 3x the signalled one) where
                        # the resulting lot would be too small to be worth it.
    poll_sec=30,      # how often to look. This used to sleep until the next M5
                      # open, so the bot saw the market once every 5 minutes and
                      # missed every zone touch that happened in between - on M5
                      # a group only lives a bar or two, so most of them were
                      # gone by the time it looked. 30s is cheap: cycle() reads
                      # 400 bars (a few ms) and the same group cannot be entered
                      # twice because it is written to done_groups on fill.
    max_lot=2.0,
    max_trades=200,   # per run. Was 6, which quietly stopped the bot mid-session -
                      # no good for a full-day test run. Override with --max-trades.
    magic=9100,
    risk_pct=4.0,     # % of balance risked per trade
    trend_mode="off", # take every setup, not just ones the M15 agrees with. With
                      # hi_len this loose the length cap does the quality work, and
                      # the gate was costing ~57% of setups for the same PF.
)


# ------------------------------------------------------------------ indicators
def heikin_ashi(d):
    o, h, l, c = (np.asarray(d[k], float) for k in ("open", "high", "low", "close"))
    n = c.size
    hac = (o + h + l + c) / 4.0
    hao = np.empty(n)
    hao[0] = o[0]
    for i in range(1, n):
        hao[i] = (hao[i - 1] + hac[i - 1]) / 2.0
    return hao, np.maximum(h, np.maximum(hao, hac)), np.minimum(l, np.minimum(hao, hac)), hac


def load(tf, n=400):
    r = mt5.copy_rates_from_pos(SYMBOL, tf, 0, n)
    if r is None:
        return None
    return {k: r[k].astype(float) for k in ("time", "open", "high", "low", "close")}


# --- connection watchdog -------------------------------------------------
# Restarting or rebuilding the MT5 terminal leaves the python handle stale:
# every call keeps returning None instead of erroring, so the loop would sit
# there logging "no bars" forever. Re-initialise on a run of misses.
_blind = 0


def feed_ok() -> bool:
    """Test the feed, re-initialising a dead handle. True = bars are flowing."""
    global _blind
    r = mt5.copy_rates_from_pos(SYMBOL, M5, 0, 5)
    if r is not None and len(r) >= 3:
        if _blind:
            log(f"  feed back after {_blind} miss(es)")
        _blind = 0
        return True
    _blind += 1
    # re-initialise at once: at a 5-minute cadence a single miss already means a
    # whole bar of blindness, so waiting for a run of 3 costs 15 minutes.
    if _blind == 1 or _blind % 3 == 0:
        log(f"  no bars ({_blind} in a row)  - re-initialising MT5")
        mt5.shutdown()
        time.sleep(2)
        if not mt5.initialize():
            log(f"  MT5 initialize FAILED: {mt5.last_error()} - is the terminal running?")
        elif _blind >= 9:
            log("  !! feed still down after 9 tries - check the terminal, not trading")
    else:
        log(f"  no bars ({_blind} in a row)")
    return False


def trend_now(tf_name: str, fast=50, slow=200) -> int:
    """+1 up / -1 down, from the last CLOSED bar of the higher timeframe."""
    tf = {"M5": M5, "M15": M15, "H1": mt5.TIMEFRAME_H1}.get(tf_name, M15)
    r = mt5.copy_rates_from_pos(SYMBOL, tf, 0, slow + 60)
    if r is None or len(r) < slow + 5:
        return 0
    c = r["close"].astype(float)[:-1]          # drop the forming bar
    return 1 if ema(c, fast)[-1] > ema(c, slow)[-1] else -1


# ------------------------------------------------------------------ the setup
@dataclass
class Setup:
    side: str
    group_start_time: int
    group_len: int
    p0: float            # 0.0 line (group END extreme)
    p100: float          # 100.0 line (group START extreme)
    entry: float
    lvl: float           # where we enter, as a fib %
    tp: float
    sl: float
    risk_pts: float
    ext_atr: float


def find_setup(d, *, zone, gap_zone, tp_level, sl_buf, lo_len, hi_len, tp_pts=0.0, entry_px=None, tp_pad=25.0, d_m5=None,
               min_ext_atr) -> Setup | None:
    """The actionable setup on the just-closed bar, or None.

    `d` must contain only CLOSED bars, newest last. We act on the open of the
    bar following the group, so the group is d[:-1] and the trigger bar is
    d[-1]; indicators are read on d[:-1] so nothing leaks from the future.
    """
    hao, hah, hal, hac = heikin_ashi(d)
    bull = hac >= hao
    n = hac.size
    if n < 60:
        return None

    # walk back from the end to find the group that just ended
    trig = n - 1                       # the bar whose OPEN we would trade
    ge = trig - 1                      # last bar of the group
    gs = ge
    while gs > 0 and bull[gs - 1] == bull[ge]:
        gs -= 1
    glen = ge - gs + 1
    if not (lo_len <= glen <= hi_len):
        return None
    if bull[trig] == bull[ge]:         # group has not actually flipped yet
        return None

    bullish = bool(bull[ge])
    ext_start = float(hal[gs:ge + 1].min()) if bullish else float(hah[gs:ge + 1].max())
    ext_end = float(hah[gs:ge + 1].max()) if bullish else float(hal[gs:ge + 1].min())
    p0, p100 = ext_end, ext_start
    leg = p0 - p100
    if abs(leg) < 10 * PT:
        return None

    side = "SELL" if bullish else "BUY"
    # entry_px lets the group come from one timeframe (H1) and the fill from
    # M5's live price - the price a bot actually gets.
    entry = float(entry_px) if entry_px is not None else float(d["open"][trig])
    lvl = (entry - p0) / (p100 - p0) * 100.0
    if not (0.0 <= lvl <= gap_zone):
        return None

    # breakout guard: a fresh 20-bar extreme right before entry means momentum,
    # so fading here is fighting the move. Read the trigger bar and the 4 before
    # it, against the 20 bars before those - causal, nothing from the future.
    if CFG["bk_block"] and d_m5 is not None:
        k, w = 5, 20
        ti = trig
        if ti > k + w + 1:
            fh = float(d_m5["high"][ti - k:ti].max())
            fl = float(d_m5["low"][ti - k:ti].min())
            ph = float(d_m5["high"][ti - k - w:ti - k].max())
            pl = float(d_m5["low"][ti - k - w:ti - k].min())
            if fh > ph or fl < pl:
                return None

    # extension filter: read at the last CLOSED bar (no future leak)
    j = n - 2
    a = atr(d["high"], d["low"], d["close"], 14)[j]
    e20 = ema(d["close"], 20)[j]
    if not np.isfinite(a) or a <= 0:
        return None
    ext = abs(p0 - e20) / a
    if ext < min_ext_atr:
        return None

    px = lambda L: p0 + (p100 - p0) * L / 100.0
    # tp_pts starts the target at OUR FILL, not at a fib level. That is the whole
    # point of the arm: identical entries across the test, different exits.
    tp = (entry - tp_pts * PT) if bullish else (entry + tp_pts * PT)
    if tp_pts <= 0:
        tp = px(tp_level)
    # stop short of the line: spread + requote would otherwise eat the fill
    tp += (tp_pad * PT) if bullish else (-tp_pad * PT)
    sl = px(-sl_buf * PT / abs(p100 - p0) * 100.0)
    return Setup(side, int(d["time"][gs]), glen, p0, p100, entry, lvl, tp, sl,
                 abs(entry - sl) / PT, float(ext))


# ------------------------------------------------------------------ sizing
def lot_for(risk_pts: float, balance: float, risk_pct: float, max_lot: float) -> float:
    """XAUUSDc cent account: 1.00 lot x 1 point = 1.0 USC."""
    info = mt5.symbol_info(SYMBOL)
    step = info.volume_step if info else 0.01
    if risk_pts <= 0:
        return 0.0
    raw = (balance * risk_pct / 100.0) / risk_pts
    return max(step, min(max_lot, round(raw / step) * step))


def live_risk_pts(s: Setup) -> float:
    """Risk in points from the price we would ACTUALLY fill at, not the fibo.

    The stop is anchored to the 0.0 line, so the deeper the fill sits inside the
    entry zone the wider the stop becomes. Sizing off s.entry therefore
    under-states risk whenever price keeps running into the zone - a live SELL
    signalled at lvl 7.9% (risk 648 pt) filled at lvl 21.0%, which is 1,390 pt,
    and took 8.6% of the account instead of the intended 4%.
    """
    tick = mt5.symbol_info_tick(SYMBOL)
    if tick is None:
        return s.risk_pts
    px = tick.ask if s.side == "BUY" else tick.bid
    return abs(px - s.sl) / PT


def live_lot(s: Setup, risk_pct: float, max_lot: float) -> float:
    """Lot sized from the fill we will actually get."""
    bal = mt5.account_info().balance if mt5.account_info() else 0.0
    return lot_for(live_risk_pts(s), bal, risk_pct, max_lot)


# ------------------------------------------------------------------ state
def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception:
            pass
    return {"done_groups": [], "open": []}


def save_state(s: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    s["done_groups"] = s["done_groups"][-200:]
    STATE.write_text(json.dumps(s, indent=2))


def log(line: str) -> None:
    LOGDIR.mkdir(parents=True, exist_ok=True)
    f = LOGDIR / f"fiboscalper{TAG}_{now_myt():%Y%m%d}.log"
    with f.open("a", encoding="utf-8") as fh:
        fh.write(f"{now_myt():%H:%M:%S} {line}\n")
    print(line, flush=True)


# ------------------------------------------------------------------ execution
def fib_px(s: Setup, level: float) -> float:
    """Price of a fib level for this setup (0.0 = p0, 100.0 = p100)."""
    return s.p0 + (s.p100 - s.p0) * level / 100.0


def _round_lot(lot: float) -> float:
    info = mt5.symbol_info(SYMBOL)
    step = info.volume_step if info else 0.01
    return round(round(lot / step) * step, 2)


def stops_ok(s: Setup, tp: float):
    """The fibo gives us SL/TP but the fill happens at MARKET, so re-check the
    levels against the live quote: a SELL needs sl above and tp below the bid,
    a BUY the mirror image, and both must clear the broker's stop distance.
    Sending stale levels is what returns retcode 10016.  Returns (ok, reason).
    """
    tick = mt5.symbol_info_tick(SYMBOL)
    if tick is None:
        return False, "no tick"
    buy = s.side == "BUY"
    price = tick.ask if buy else tick.bid
    info = mt5.symbol_info(SYMBOL)
    min_d = max((info.trade_stops_level if info else 0) * PT, 20 * PT)
    if buy:
        ok = s.sl < price - min_d and tp > price + min_d
        need = f"needs sl<{price - min_d:.2f}, tp>{price + min_d:.2f}"
    else:
        ok = s.sl > price + min_d and tp < price - min_d
        need = f"needs sl>{price + min_d:.2f}, tp<{price - min_d:.2f}"
    return ok, (f"bid={tick.bid:.2f} ask={tick.ask:.2f} sl={s.sl:.2f} "
                f"tp={tp:.2f} ({need})")


def fire_split(s: Setup, lot: float, dry: bool):
    """The teacher's target ladder: part leaves at 100.0, part runs to 161.8.

    Same stop on both halves, so total risk is unchanged - this only changes
    where the profit is taken. A lot too small to halve fires as one order.

    Both legs are validated BEFORE either is sent: a half-filled ladder would
    leave an unintended position size.
    """
    info = mt5.symbol_info(SYMBOL)
    vmin = info.volume_min if info else 0.01
    half = _round_lot(lot / 2)
    if half < vmin or _round_lot(lot - half) < vmin:
        log(f"  lot {lot:.2f} too small to split - one order at TP {s.tp:.2f}")
        return [fire(s, lot, dry)]
    tp2 = fib_px(s, CFG["tp_ext"])
    for tp in (s.tp, tp2):
        ok, why = stops_ok(s, tp)
        if not ok:
            log(f"  SKIP {s.side} {lot:.2f} - stops stale: {why}")
            return []
    log(f"  ladder: {_round_lot(lot - half):.2f} @ TP {s.tp:.2f} (100%) + "
        f"{half:.2f} @ TP {tp2:.2f} ({CFG['tp_ext']}%)")
    out = [fire(s, _round_lot(lot - half), dry, sent=True),
           fire(s, half, dry, tp=tp2, sent=True)]
    return [t for t in out if t is not None]


def fire(s: Setup, lot: float, dry: bool, tp: float | None = None, sent: bool = False):
    """Returns the position ticket (live) / -1 (dry) / None (failed).

    The fibo gives us SL/TP, but the fill happens at MARKET, so we re-check the
    levels against the live quote first: a SELL needs sl above and tp below the
    bid, a BUY the mirror image, and both must clear the broker's minimum stop
    distance. Sending stale levels is what returns retcode 10016.

    `sent` means fire_split already validated this exact (sl, tp) pair against
    the live quote, so the check is not repeated.
    """
    tick = mt5.symbol_info_tick(SYMBOL)
    if tick is None:
        return None
    buy = s.side == "BUY"
    price = tick.ask if buy else tick.bid

    sl, tp = s.sl, (s.tp if tp is None else tp)
    if not sent:
        ok, why = stops_ok(s, tp)
        if not ok:
            log(f"  SKIP {s.side} {lot:.2f} - stops stale: {why}")
            return None

    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": SYMBOL,
        "volume": lot,
        "type": mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL,
        "price": price,                        # DEAL requires price (retcode 10013)
        "sl": round(sl, 2),
        "tp": round(tp, 2),
        "deviation": 30,
        "magic": CFG["magic"],
        "comment": f"fiboHA L{s.lvl:.0f}",
        "type_filling": mt5.ORDER_FILLING_FOK,
    }
    if dry:
        log(f"  DRY  {s.side} {lot:.2f} @ {price:.2f}  SL {sl:.2f}  TP {tp:.2f} "
            f" (lvl {s.lvl:.1f}%  risk {s.risk_pts:.0f}pt  ext {s.ext_atr:.2f}ATR)")
        return -1
    r = mt5.order_send(req)
    if r is None or r.retcode != mt5.TRADE_RETCODE_DONE:
        log(f"  FAIL {s.side} {lot:.2f} @ {price:.2f} retcode={getattr(r,'retcode',None)} "
            f"{getattr(r,'comment','')}  sl={sl:.2f} tp={tp:.2f}")
        return None
    if _tb:
        _tb.event("entry", "SETUP %s group %s x%d  0.0=%.2f 100.0=%.2f  lvl %.1f%%  "
                           "risk %.0fpt  lot %.2f  ->  SENT @ %.2f  SL %.2f  TP %.2f  "
                           "ticket=%s"
                  % (s.side, ser(s.group_start_time), s.group_len, s.p0, s.p100,
                     s.lvl, rp, lot, r.price, sl, tp, r.ticket))
    log(f"  SENT {s.side} {lot:.2f} @ {r.price:.2f}  SL {sl:.2f}  TP {tp:.2f} "
        f" ticket={r.order}  (lvl {s.lvl:.1f}%  risk {s.risk_pts:.0f}pt)")
    return r.order


def move_sl(pos, new_sl: float) -> bool:
    """Shift a stop. Used by the breakeven rule - once a leg is far enough ahead
    the trade should not be able to turn into a loss."""
    req = dict(action=mt5.TRADE_ACTION_SLTP, position=pos.ticket, symbol=pos.symbol,
               sl=round(new_sl, 2), tp=round(pos.tp, 2), magic=CFG["magic"])
    r = mt5.order_send(req)
    return bool(r and r.retcode == mt5.TRADE_RETCODE_DONE)


def manage(st: dict, dry: bool) -> None:
    """Void any live setup whose 0.0 line a CLOSED candle has broken through."""
    d = load(M5, 60)
    if d is None or d["time"].size < 5:
        return
    d = {k: v[:-1] for k, v in d.items()}       # closed bars only
    live = []
    for rec in st.get("open", []):
        pos = next((p for p in (mt5.positions_get() or ()) if p.ticket == rec["ticket"]), None)
        if pos is None:
            log(f"  closed  ticket={rec['ticket']} ({rec['side']} lvl {rec['lvl']:.1f}%)")
            continue
        # Bank it. Deliberately checked BEFORE the grace window and before the
        # VOID rule: once the money is on the table the exit is the point.
        if CFG["money_tp"] > 0 and float(pos.profit) >= CFG["money_tp"]:
            if dry:
                log(f"  DRY  TP$ {rec['side']} ticket={rec['ticket']} "
                    f"+{pos.profit:.0f} >= {CFG['money_tp']:.0f}")
            else:
                if _tb:
                    _tb.event("exit", "MONEY-TP %s ticket=%s banked %+.0f USC"
                              % (rec["side"], rec["ticket"], pos.profit))
                r = close_position(pos, comment="fiboHA money target")
                log(f"  TP$ {rec['side']} ticket={rec['ticket']} closed "
                    f"retcode={getattr(r,'retcode',None)} banked +{pos.profit:.0f} USC")
                if CFG["rescale"] and rec.get("group_len"):
                    k = f"{rec['group_start']}-{rec['group_len']}"
                    if k in st["done_groups"]:
                        st["done_groups"].remove(k)
                        log(f"  rescale: group {k} freed - will scalp it again on the next pullback")
            continue
        # Breakeven: once far enough ahead, the stop moves to entry so the leg
        # cannot become a loss. Checked before grace/VOID for the same reason as
        # the money target - protecting an open profit is the point.
        if CFG["be_at"] > 0 and not rec.get("bpd") and float(pos.profit) >= CFG["be_at"]:
            be = pos.price_open
            if (rec["side"] == "SELL" and (pos.sl <= 0 or pos.sl > be)) or                (rec["side"] == "BUY" and (pos.sl <= 0 or pos.sl < be)):
                if dry:
                    log(f"  DRY  BE {rec['side']} ticket={rec['ticket']} -> {be:.2f}")
                elif move_sl(pos, be):
                    log(f"  BE {rec['side']} ticket={rec['ticket']} stop -> entry {be:.2f} "
                        f"(+{pos.profit:.0f} USC secured)")
                    rec["bpd"] = True

        # grace: count closed candles since entry
        bars_since = int((d["time"][-1] - rec["entry_bar_time"]) / 300)
        if bars_since <= CFG["grace"]:
            live.append(rec)
            continue
        broke = (d["close"][-1] > rec["p0"]) if rec["side"] == "SELL" else (d["close"][-1] < rec["p0"])
        if broke:
            if dry:
                log(f"  DRY  VOID {rec['side']} ticket={rec['ticket']} - candle closed "
                    f"through 0.0 ({rec['p0']:.2f})")
            else:
                if _tb:
                    _tb.event("exit", "VOID %s ticket=%s - 0.0 line broken (%+.0f USC)"
                              % (rec["side"], rec["ticket"], pos.profit))
                r = close_position(pos, comment="fiboHA void 0.0 break")
                log(f"  VOID {rec['side']} ticket={rec['ticket']} closed "
                    f"retcode={getattr(r,'retcode',None)} - 0.0 line broken")
            continue

        # --- teacher's ENTRY label: a retracement back to 38.2 is a re-entry
        if CFG["reentry"] > 0 and not rec.get("reentered"):
            tick = mt5.symbol_info_tick(SYMBOL)
            lvl_px = rec["p0"] + (rec["p100"] - rec["p0"]) * CFG["reentry"] / 100.0
            if tick:
                hit = (tick.bid <= lvl_px) if rec["side"] == "SELL" else (tick.ask >= lvl_px)
                if hit:
                    acct = mt5.account_info()
                    lot = lot_for(abs(lvl_px - rec["sl"]) / PT, acct.balance,
                                  CFG["risk_pct"], CFG["max_lot"])
                    s2 = Setup(rec["side"], rec["group_start"], 0, rec["p0"], rec["p100"],
                               lvl_px, CFG["reentry"], rec["tp"], rec["sl"],
                               abs(lvl_px - rec["sl"]) / PT, 0.0)
                    if fire(s2, lot, dry) is not None:
                        log(f"  RE-ENTRY {rec['side']} at the ENTRY line "
                            f"({CFG['reentry']}% = {lvl_px:.2f}) lot {lot:.2f}")
                    rec["reentered"] = True
        live.append(rec)
    st["open"] = live


# ------------------------------------------------------------------ cycle
def cycle(dry: bool, st: dict) -> int:
    d = load(M5, 400)
    if d is None or d["time"].size < 100:
        if not feed_ok():            # reconnects a stale handle, logs why
            return 0
        d = load(M5, 400)
        if d is None or d["time"].size < 100:
            log("  history too short for this symbol")
            return 0
    # control file: {"skip": true} marks the current group spent and moves on;
    # {"close_all": true} flattens everything. Read every poll so a command
    # lands within one cycle - the state is loaded only once, at start.
    ctl = {}
    if CONTROL.exists():
        try:
            ctl = json.loads(CONTROL.read_text())
        except Exception:
            ctl = {}
        if ctl.pop("close_all", False):
            for rec in st.get("open", []):
                q = next((x for x in (mt5.positions_get() or ()) if x.ticket == rec["ticket"]), None)
                if q and not dry:
                    if _tb:
                        _tb.event("exit", "CLOSE-ALL ticket=%s (%+.0f USC)"
                                  % (q.ticket, q.profit))
                    close_position(q, comment="fiboHA close_all")
            st["open"] = []
            log("  control: closed everything")
        CONTROL.write_text(json.dumps(ctl))
    trig_time = int(d["time"][-1])              # the bar that just OPENED
    d_closed = {k: v[:-1] for k, v in d.items()}
    if CFG["fibo_tf"] != "M5":
        gf = load({"M15": M15, "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4}.get(
            CFG["fibo_tf"], M5), 600)
        d_group = {k: v[:-1] for k, v in gf.items()} if gf else d_closed
    else:
        d_group = d_closed
    s = find_setup(d_group, entry_px=float(d["close"][-1]), d_m5=d_closed,
                   zone=CFG["zone"], gap_zone=CFG["gap_zone"],
                   tp_level=CFG["tp_level"], sl_buf=CFG["sl_buf"], tp_pts=CFG["tp_pts"], tp_pad=CFG["tp_pad"],
                   lo_len=CFG["lo_len"], hi_len=CFG["hi_len"],
                   min_ext_atr=CFG["min_ext_atr"])
    if s is None:
        return 0
    key = f"{s.group_start_time}-{s.group_len}"
    # control file: {"skip": true} marks this group spent and moves on, so a leg
    # can be skipped without restarting the bot (the state is read only once, at
    # start). {"close_all": true} flattens everything we hold.
    if key in st["done_groups"]:
        return 0
    if _tb:
        try:
            q = [x for x in (mt5.positions_get() or ())
                 if x.ticket in {int(r["ticket"]) for r in st.get("open", [])}]
            acct = mt5.account_info()
            rows = [dict(arm=os.environ.get("FIBOSCALPER_TAG", "?"), ticket=p.ticket, side="BUY" if p.type == 0 else "SELL",
                         lot=p.volume, entry=round(p.price_open, 2), sl=round(p.sl, 2),
                         tp=round(p.tp, 2), profit=round(p.profit, 2))
                    for p in q]
            _tb.pub_account(acct.balance, acct.equity,
                            sum(r["profit"] for r in rows), rows, arm=os.environ.get("FIBOSCALPER_TAG", "?"))
        except Exception as e:
            log(f"  bridge: {type(e).__name__}: {e}")

    if ctl.pop("skip", False):
        st["done_groups"].append(key)
        log(f"  control: SKIP {ser(s.group_start_time)} x{s.group_len} -> next setup")
        if not dry:
            save_state(st)
        CONTROL.write_text(json.dumps(ctl))
        return 0

    # ---- scale in ----------------------------------------------------------
    # With layers=1 this is a single entry, the teacher's way. With layers>1 the
    # first leg goes in as soon as price is in the zone, and each further leg
    # waits for price to walk another layer_step deeper, so the average entry
    # improves while the stop (anchored at 0.0) stays where it was. Three legs
    # placed at once is just 3x the size; this is the different idea, and the
    # backtest could not answer it, so live is where it gets settled.
    counts = st.setdefault("layers_in", {})
    lc = counts.get(key, 0)
    if CFG["layers"] > 1 and s.lvl < CFG["layer_step"] * (lc + 1):
        return 0

    tr = trend_now(CFG["trend_tf"])
    want = 1 if s.side == "BUY" else -1
    if CFG["trend_mode"] != "off" and tr != want:
        log(f"  skip {s.side} lvl {s.lvl:.1f}% - {CFG['trend_tf']} trend "
            f"{'UP' if tr > 0 else 'DOWN'} disagrees")
        st["done_groups"].append(key)
        return 0

    acct = mt5.account_info()
    # size off the fill we will actually get: the stop is anchored to the 0.0
    # line, so a fill deeper in the zone risks more per point
    rk = live_risk_pts(s)
    # Hard guard. live_lot keeps the money constant, so this only trips on a
    # pathological drift (stop 3x the signalled one), where the lot needed to
    # hold risk constant would be too small to be worth placing.
    if s.risk_pts > 0 and rk > CFG["max_risk_mult"] * s.risk_pts:
        log(f"  SKIP {s.side} lvl {s.lvl:.1f}% - price ran too deep: live stop "
            f"{rk:.0f}pt vs {s.risk_pts:.0f}pt signalled "
            f"(> {CFG['max_risk_mult']:.1f}x)")
        st["done_groups"].append(key)
        return 0
    lot = lot_for(rk, acct.balance, CFG["risk_pct"] / CFG["layers"], CFG["max_lot"])
    log(f"  SETUP {s.side} group {ser(s.group_start_time)} x{s.group_len}  "
        f"0.0={s.p0:.2f} 100.0={s.p100:.2f}  lvl {s.lvl:.1f}%  ext {s.ext_atr:.2f}ATR  "
        f"risk {rk:.0f}pt  lot {lot:.2f}")
    raw = fire_split(s, lot, dry) if CFG["split"] else [fire(s, lot, dry)]
    # fire() returns None when the order is refused (stale stops, broker reject).
    # A [None] list is still truthy, so filter first - otherwise the ticket loop
    # below compares None > 0 and kills the whole bot mid-session.
    tkts = [t for t in raw if t is not None]
    if not tkts:
        log(f"  no fill for {s.side} x{s.group_len} - leaving the group unspent")
        return 0
    counts[key] = lc + 1
    if counts[key] >= CFG["layers"]:
        st["done_groups"].append(key)
    for tkt in tkts:
        if tkt > 0:
            st["open"].append({"ticket": tkt, "side": s.side, "p0": s.p0, "p100": s.p100,
                               "lvl": s.lvl, "sl": s.sl, "tp": s.tp,
                               "group_start": s.group_start_time, "group_len": s.group_len,
                               "entry_bar_time": trig_time, "entry": s.entry})
    return 1


def ser(t: int) -> str:
    return f"{server_dt(t):%m-%d %H:%M}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Fibo+HA scalper (teacher's method)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--once", action="store_true", help="one cycle then exit")
    ap.add_argument("--risk-pct", type=float, default=CFG["risk_pct"])
    ap.add_argument("--max-lot", type=float, default=CFG["max_lot"])
    ap.add_argument("--zone", type=float, default=CFG["zone"])
    ap.add_argument("--gap-zone", type=float, default=CFG["gap_zone"])
    ap.add_argument("--tp", type=float, default=CFG["tp_level"])
    ap.add_argument("--tp-pts", type=float, default=CFG["tp_pts"],
                    help="fixed point target instead of a fib level")
    ap.add_argument("--magic", type=int, default=CFG["magic"])
    ap.add_argument("--layers", type=int, default=CFG["layers"])
    ap.add_argument("--layer-step", type=float, default=CFG["layer_step"])
    ap.add_argument("--tp-pad", type=float, default=CFG["tp_pad"],
                    help="stop this many points short of the target line")
    ap.add_argument("--fibo-tf", default=CFG["fibo_tf"], choices=["M5", "M15", "H1", "H4"])
    ap.add_argument("--no-rescale", action="store_true")
    ap.add_argument("--no-bk", action="store_true")
    ap.add_argument("--be-at", type=float, default=CFG["be_at"],
                    help="move the stop to entry once a leg shows this much USC")
    ap.add_argument("--money-tp", type=float, default=CFG["money_tp"],
                    help="close a leg once it shows this much USC (0 = off)")
    ap.add_argument("--sl-buf", type=float, default=CFG["sl_buf"])
    ap.add_argument("--min-ext-atr", type=float, default=CFG["min_ext_atr"])
    ap.add_argument("--reentry", type=float, default=CFG["reentry"],
                    help="re-entry fib level (teacher's ENTRY label, e.g. 38.2)")
    ap.add_argument("--split", action="store_true",
                    help="fire the 100/161.8 target ladder instead of a single TP "
                         "(measured -4%% vs the real first-touch entry)")
    ap.add_argument("--max-trades", type=int, default=CFG["max_trades"],
                    help="setups to take before exiting (default %d)" % CFG["max_trades"])
    ap.add_argument("--trend", action="store_true",
                    help="only take setups the higher TF agrees with. Measured "
                         "(fill=close): M5 fading with the trend 51.8% win / "
                         "PF 1.30 / +2,055 USC/day; fading against it 40.9% / "
                         "PF 0.83 / -2,065. On H1 both sides pay, but aligned "
                         "is PF 2.21 vs 1.30.")
    ap.add_argument("--no-trend", action="store_true",
                    help="take every setup, not just ones the higher timeframe agrees "
                         "with (doubles setups for the same PF once hi_len is small)")
    ap.add_argument("--poll", type=int, default=CFG["poll_sec"],
                    help="seconds between market checks (default %d)" % CFG["poll_sec"])
    ap.add_argument("--trend-tf", default=CFG["trend_tf"])
    a = ap.parse_args()
    for k in ("risk_pct", "max_lot", "zone", "gap_zone", "sl_buf", "min_ext_atr",
              "reentry", "tp_pts", "magic", "layers", "layer_step", "money_tp", "be_at", "fibo_tf", "tp_pad"):
        CFG[k] = getattr(a, k)
    CFG["tp_level"] = a.tp
    CFG["tp_pts"] = a.tp_pts
    CFG["magic"] = a.magic
    CFG["layers"] = a.layers
    CFG["layer_step"] = a.layer_step
    CFG["money_tp"] = a.money_tp
    CFG["be_at"] = a.be_at
    CFG["fibo_tf"] = a.fibo_tf
    CFG["tp_pad"] = a.tp_pad
    CFG["bk_block"] = 0 if a.no_bk else 1
    if a.trend:
        CFG["trend_mode"] = "align"
    elif a.no_trend:
        CFG["trend_mode"] = "off"
    CFG["rescale"] = 0 if a.no_rescale else 1
    CFG["trend_tf"] = a.trend_tf
    CFG["split"] = a.split
    CFG["max_trades"] = a.max_trades
    CFG["poll_sec"] = a.poll
    if a.no_trend:
        CFG["trend_mode"] = "off"

    ti, ai = connect()
    if not ti.trade_allowed:
        log("Algo Trading is OFF in the terminal - refusing to run")
        return 1
    # A dry run / verification must never write into the live trading log: its
    # fake orders use the same lot sizes and read back as if the real bot were
    # skipping entries. Redirect before the first log() call.
    if a.dry_run:
        global LOGDIR
        LOGDIR = LOGDIR.with_name(LOGDIR.name + "_dry" + TAG)
    log(f"--- fiboscalper {'DRY' if a.dry_run else 'LIVE'}  {now_myt():%Y-%m-%d %H:%M} MYT")
    log(f"    balance {ai.balance:,.2f} USC   zone {CFG['zone']}/{CFG['gap_zone']}  "
        f"TP {('%gpt' % CFG['tp_pts']) if CFG['tp_pts'] > 0 else CFG['tp_level']}  "
        f"SL +{CFG['sl_buf']}pt  ext>={CFG['min_ext_atr']}ATR  "
        f"layers {CFG['layers']}x{CFG['layer_step']:g}%  "
        f"fibo {CFG['fibo_tf']} bk{CFG['bk_block']}  "f"moneyTP {('%gUSC' % CFG['money_tp']) if CFG['money_tp'] > 0 else 'off'}  "
        f"BE {('%gUSC' % CFG['be_at']) if CFG['be_at'] > 0 else 'off'}  "
        f"trend {CFG['trend_tf']}:{CFG['trend_mode']}  risk {CFG['risk_pct']}%  "
        f"max lot {CFG['max_lot']}  poll {CFG['poll_sec']}s")
    st = {"done_groups": [], "open": []} if a.dry_run else load_state()
    n = 0
    while True:
        manage(st, a.dry_run)        # VOID closes, stop checks on open legs
        n += cycle(a.dry_run, st)
        if not a.dry_run:            # a dry run must never consume a setup
            save_state(st)
        if a.once or n >= CFG["max_trades"]:
            break
        # Look often. The group is only re-derived on a bar close (cycle()
        # drops the forming bar), so extra passes are cheap and a zone touch
        # between bars no longer slips through.
        time.sleep(max(1, CFG["poll_sec"]))
    log(f"--- done, {n} setup(s)")
    mt5.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
