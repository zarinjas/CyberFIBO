"""Regression suite for the fibo+Heikin-Ashi scalper.

Run it with the project venv:

    C:/Users/afroh/workspace/sandbox/mt5-venv/Scripts/python.exe -m pytest tests -q

Tests that need live data are skipped when MetaTrader 5 is not answering, so the
suite stays green on a machine with the terminal closed.
"""
from __future__ import annotations

import inspect
import os
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# The suite calls fire()/fire_split() with the real lot sizes, and those log.
# Without this the test output lands in the LIVE trading log and reads back as
# the bot skipping entries it never saw. Must be set before importing fiboscalper.
os.environ.setdefault("FIBOSCALPER_LOGDIR", tempfile.mkdtemp(prefix="hermes-test-log-"))

import MetaTrader5 as mt5            # noqa: E402
import fiboscalper as S              # noqa: E402
import fibo_ha_backtest as F         # noqa: E402
import fibo_edge_study as E          # noqa: E402
import mt5lib                        # noqa: E402


# ------------------------------------------------------------------ fixtures
def _mt5_up() -> bool:
    try:
        return bool(mt5.initialize())
    except Exception:
        return False


LIVE = _mt5_up()
needs_mt5 = pytest.mark.skipif(not LIVE, reason="MetaTrader 5 not answering")


@pytest.fixture(scope="session")
def bars():
    if not LIVE:
        pytest.skip("MetaTrader 5 not answering")
    d = S.load(S.M5, 20000)
    if d is None or d["time"].size < 1000:
        pytest.skip("not enough history")
    return d


@pytest.fixture(scope="session")
def ha(bars):
    return F.heikin_ashi(bars)


@pytest.fixture(scope="session")
def runs(bars, ha):
    ho, hh, hl, hc = ha
    t5 = np.asarray(bars["time"], dtype=np.int64)
    tr = F.htf_trend(t5, mt5.TIMEFRAME_M15)
    K = dict(tp_level=100.0, zone=23.6, gap_zone=38.2, sl_buf=200, grace_bars=2,
             sequential=True, trend=tr, trend_mode="align", cycle_lock=True)
    return lambda **kw: F.run(bars, ho, hh, hl, hc, **{**K, **kw})


def stats(ts):
    a = np.array([t.pts for t in ts])
    rk = np.array([t.risk_pts for t in ts])
    rr = a / np.maximum(rk, 1e-9)
    w, l = a[a > 0].sum(), abs(a[a <= 0].sum())
    return dict(n=len(a), win=100 * (a > 0).mean(),
                pf=w / l if l else float("inf"), er=rr.mean())


# ------------------------------------------------------------------ config
class TestConfig:
    """The filters settled on 2026-09-16. If one changes, change the test too."""

    def test_group_length_cap(self):
        """1 means a lone candle counts; the upper bound is deliberately loose so
        the bot keeps trading during trends, when long groups are the only ones
        forming. A 16-candle group with price inside its zone was being skipped."""
        assert S.CFG["lo_len"] == 1
        assert S.CFG["hi_len"] >= 20, S.CFG["hi_len"]

    def test_extension_filter_off(self):
        assert S.CFG["min_ext_atr"] == 0.0

    def test_lone_candle_group_allowed(self):
        assert S.CFG["lo_len"] == 1

    def test_entry_and_target_unchanged(self):
        assert S.CFG["zone"] == 23.6
        assert S.CFG["gap_zone"] == 50.0
        assert S.CFG["tp_level"] == 100.0
        assert S.CFG["sl_buf"] == 200

    def test_split_target_is_off(self):
        assert S.CFG["split"] is False

    def test_run_length_is_a_full_session(self):
        assert S.CFG["max_trades"] >= 100

    def test_trend_gate_can_be_switched_off(self):
        """--no-trend must bypass the gate, not silently still apply it. The live
        config runs with it off; the flag is what the default encodes."""
        assert S.CFG["trend_mode"] == "off"
        assert 'CFG["trend_mode"] != "off" and tr != want' in inspect.getsource(S.cycle)
        assert "CFG[\"trend_mode\"] = \"off\"" in inspect.getsource(S.main)
        assert "--no-trend" in inspect.getsource(S.main)

    def test_an_ab_arm_can_override_the_exit_and_its_identity(self):
        """Three bots enter the same setup and differ only in the exit, so each
        needs its own magic (separate positions) and its own state file
        (separate done_groups). The fixed target starts at OUR FILL, not at a fib
        level - that is what makes the arms comparable."""
        assert S.CFG["tp_pts"] == 0.0, "default must stay the fib-level exit"
        src = inspect.getsource(S.find_setup)
        assert "if tp_pts <= 0:" in src and "tp = px(tp_level)" in src
        assert "(entry - tp_pts * PT) if bullish" in src
        body = (ROOT / "fiboscalper.py").read_text(encoding="utf-8")
        assert 'os.environ.get("FIBOSCALPER_TAG"' in body
        assert 'os.environ.get("FIBOSCALPER_STATE"' in body
        assert "fiboscalper{TAG}" in body, "state file must be per-arm"
        assert '--tp-pts' in body and '--magic' in body

    def test_the_main_loop_still_manages_open_positions(self):
        """manage() carries the VOID rule and the stop checks. A patch of mine
        silently dropped the call, which would have left every open leg
        unmanaged - so it is now pinned."""
        src = inspect.getsource(S.main)
        assert "manage(st, a.dry_run)" in src, "manage() must run every loop"
        assert "n += cycle(a.dry_run, st)" in src
        assert "save_state(st)" in src
        assert src.index("manage(st, a.dry_run)") < src.index("n += cycle")
        assert "if not a.dry_run:" in src, "a dry run must never consume a setup"

    def test_scale_in_waits_for_price_to_go_deeper(self):
        """layers>1 must add a leg only once price has walked another layer_step
        into the zone, and must not mark the group spent until the last leg is in.
        Placing three legs at once is just 3x the size - that was already measured
        and has no edge; entering INTO the move is the untested idea."""
        body = (ROOT / "fiboscalper.py").read_text(encoding="utf-8")
        assert S.CFG["layers"] == 1, "default stays the teacher's single entry"
        assert S.CFG["layer_step"] == 12.5
        src = inspect.getsource(S.cycle)
        assert 'if CFG["layers"] > 1 and s.lvl < CFG["layer_step"] * (lc + 1):' in src,             "leg N must wait for N steps deep, not N-1"
        assert 'CFG["risk_pct"] / CFG["layers"]' in inspect.getsource(S.cycle),             "layers must split the risk, not multiply it"
        assert 'counts[key] = lc + 1' in src
        assert 'if counts[key] >= CFG["layers"]:' in src, "must not close the group early"
        assert '--layers' in body and '--layer-step' in body
        assert 'CFG["layers"] = a.layers' in body and 'CFG["layer_step"] = a.layer_step' in body
        # the group is only spent after the FINAL leg, never on the first
        assert src.index("counts[key] = lc + 1") < src.index('st["done_groups"].append(key)\n    for tkt')

    def test_money_target_banks_a_leg_before_anything_else(self):
        """The teacher's scalp exit: close once the leg SHOWS the target profit,
        whatever the leg length. It must run before the grace window and before
        the VOID rule - once the money is on the table, the exit is the point.

        Measured with honest fills (trigger-bar close), per position:
        +100 -> -103 USC/day, +300 -> 112, +500 -> 193, +700 -> 246, against
        fibo 100.0 -> 66. Too tight harvests noise, so the default is OFF and the
        arm that tests it uses +500."""
        assert S.CFG["money_tp"] == 0.0, "default must not change the teacher's exit"
        src = inspect.getsource(S.manage)
        assert 'CFG["money_tp"] > 0 and float(pos.profit) >= CFG["money_tp"]' in src
        assert 'close_position(pos, comment="fiboHA money target")' in src
        # ordering: the banking check runs before the grace window and the VOID rule
        assert src.index('float(pos.profit) >= CFG["money_tp"]') < src.index('CFG["grace"]')
        assert src.index('float(pos.profit) >= CFG["money_tp"]') < src.index('0.0 line broken')
        assert "money_tp" in inspect.getsource(S.main)
        assert '"--money-tp"' in (ROOT / "fiboscalper.py").read_text(encoding="utf-8")

    def test_cfg_matches_what_is_running_live(self):
        """The bot's CFG is the single source of truth - the watchdog passes no
        flags. Hard-coded watchdog args once restarted it at risk 1% with the
        M15 gate back on, silently undoing the live tuning."""
        assert S.CFG["risk_pct"] == 4.0, S.CFG["risk_pct"]
        assert S.CFG["max_lot"] == 2.0, S.CFG["max_lot"]
        assert S.CFG["trend_mode"] == "off"
        assert S.CFG["poll_sec"] == 30
        assert S.CFG["hi_len"] == 40
        wd = Path(r"C:\Users\afroh\AppData\Local\hermes\scripts\fiboscalper_watchdog.py")
        if wd.exists():
            body = wd.read_text(encoding="utf-8")
            assert "ARGS: list[str] = []" in body, "watchdog must not pass overrides"
            assert "--risk-pct" not in body, "watchdog hard-codes a flag again"

    def test_it_looks_at_the_market_more_than_once_a_bar(self):
        """Sleeping until the next M5 open threw away every zone touch in between.

        On M5 a group lives a bar or two, so a 5-minute blind spot lost most of
        them: the market offered ~22 eligible entries a day and the bot took 11.
        """
        assert S.CFG["poll_sec"] <= 60, S.CFG["poll_sec"]
        src = inspect.getsource(S.main)
        assert 'time.sleep(max(1, CFG["poll_sec"]))' in src
        assert "// 300 + 1) * 300" not in src, "still sleeping to the next bar"
        assert "--poll" in src and 'CFG["poll_sec"] = a.poll' in src

    def test_one_setup_no_longer_blocks_the_next(self):
        """The live loop never had a lock: check() only skips groups already in
        done_groups, and manage() walks every position. sequential/cycle_lock are
        BACKTESTER flags - they used to sit unused in CFG, which read as if the
        bot were serialised. Guard against them creeping back in here."""
        assert "sequential" not in S.CFG and "cycle_lock" not in S.CFG
        src = inspect.getsource(S.cycle)
        assert "done_groups" in src, "the only skip rule should be done_groups"
        assert "sequential" not in src and "cycle_lock" not in src


# ------------------------------------------------------------------ execution
class TestExecution:
    def test_fib_px_maps_the_anchors(self):
        s = S.Setup("SELL", 0, 1, 4300.0, 4290.0, 4300.0, 0.0, 4290.0, 4302.0, 200.0, 1.0)
        assert S.fib_px(s, 0.0) == pytest.approx(4300.0)
        assert S.fib_px(s, 100.0) == pytest.approx(4290.0)
        assert S.fib_px(s, 161.8) < 4290.0          # a SELL runner sits lower

    @needs_mt5
    def test_stale_stops_are_refused_both_ways(self):
        t = mt5.symbol_info_tick(S.SYMBOL)
        mkt = (t.bid + t.ask) / 2
        bad_sell = S.Setup("SELL", 0, 1, mkt + 2, mkt - 3, mkt, 0.0, mkt + 2, mkt + .5, 300, 1)
        bad_buy = S.Setup("BUY", 0, 1, mkt - 2, mkt + 3, mkt, 0.0, mkt - 2, mkt - .5, 300, 1)
        assert S.stops_ok(bad_sell, bad_sell.tp)[0] is False
        assert S.stops_ok(bad_buy, bad_buy.tp)[0] is False

    @needs_mt5
    def test_a_valid_setup_passes_the_guard(self):
        t = mt5.symbol_info_tick(S.SYMBOL)
        mkt = (t.bid + t.ask) / 2
        good = S.Setup("SELL", 0, 1, mkt + 2, mkt - 3, mkt, 0.0, mkt - 1, mkt + 1, 300, 1)
        assert S.stops_ok(good, good.tp)[0] is True

    @needs_mt5
    def test_split_ladder_is_atomic(self, monkeypatch):
        """One stale leg must stop BOTH orders - a half fill is an unintended size."""
        t = mt5.symbol_info_tick(S.SYMBOL)
        mkt = (t.bid + t.ask) / 2
        # a valid SELL shape, then make the MAIN target stale for a SELL
        s = S.Setup("SELL", 0, 1, mkt + 2, mkt - 3, mkt, 0.0, mkt - 1, mkt + 1, 300, 1)
        s.tp = mkt + 2.0                       # above the bid -> stale
        assert S.stops_ok(s, S.fib_px(s, 161.8))[0] is True, "the runner alone IS valid"
        sent = []
        monkeypatch.setattr(S, "fire", lambda *a, **k: sent.append(1) or 1)
        assert S.fire_split(s, 0.20, True) == []
        assert sent == [], "nothing may be sent when one leg is stale"

    def test_the_guard_lives_in_one_place(self):
        assert "stops_ok" in inspect.getsource(S.fire)
        assert "stops_ok" in inspect.getsource(S.fire_split)

    def test_cycle_sends_a_single_order_by_default(self):
        assert 'fire_split(s, lot, dry) if CFG["split"] else [fire(s, lot, dry)]' \
            in inspect.getsource(S.cycle)

    def test_a_refused_order_does_not_crash_the_bot(self, monkeypatch):
        """fire() returns None on a refused order, so the ticket list becomes
        [None] - still truthy. The loop then compared None > 0 and killed the
        whole process mid-session, leaving open positions unmanaged. Filter it."""
        src = inspect.getsource(S.cycle)
        assert "raw = fire_split(s, lot, dry) if CFG[\"split\"] else [fire(s, lot, dry)]" in src
        assert "tkts = [t for t in raw if t is not None]" in src
        assert "if not tkts:" in src and "return 0" in src
        # and the guard must come before the loop that would do None > 0
        assert src.index("t is not None") < src.index("for tkt in tkts")

    def test_cycle_survives_a_none_ticket_for_real(self, monkeypatch):
        """End-to-end: force fire() to refuse, run a cycle, expect no exception."""
        monkeypatch.setattr(S, "fire", lambda *a, **k: None)
        n = S.cycle(True, {"done_groups": [], "open": []})
        assert n in (0, 1), n

    def test_the_lot_is_sized_from_the_fill_not_the_fibo(self):
        """A fill deeper in the zone means a wider stop (it is anchored to 0.0),
        so sizing off s.entry under-states risk. Measured live: signalled at lvl
        7.9% for 648 pt, filled at lvl 21.0% for 1,390 pt - 8.6% of the account
        instead of 4%."""
        assert "live_risk_pts" in inspect.getsource(S.cycle)
        src = inspect.getsource(S.live_risk_pts)
        assert "symbol_info_tick" in src and "abs(px - s.sl)" in src
        assert "lot_for(live_risk_pts(s)" in inspect.getsource(S.live_lot)
        # the guard must be a loose last resort, not a second filter
        assert S.CFG["max_risk_mult"] >= 2.0, S.CFG["max_risk_mult"]
        assert 'CFG["max_risk_mult"] * s.risk_pts' in inspect.getsource(S.cycle)


# ------------------------------------------------------------------ indicators
class TestIndicators:
    def test_rsi_is_bounded(self, bars):
        r = mt5lib.rsi(bars["close"], 14)
        assert not np.isnan(r).any()
        assert 0.0 <= r.min() and r.max() <= 100.0
        assert 35 < np.median(r) < 65

    def test_adx_is_bounded(self, bars):
        a, pdi, ndi = mt5lib.adx(bars["high"], bars["low"], bars["close"], 14)
        assert 0.0 <= np.nanmin(a) and np.nanmax(a) <= 100.0

    def test_one_adx_implementation(self):
        """The studies must delegate, not carry their own copy."""
        for f in ("fibo_layers_backtest.py", "fibo_trend_study.py", "fibo_edge_study.py"):
            src = (ROOT / f).read_text(encoding="utf-8")
            assert "def wilder(" not in src, f
        assert "mt5lib_adx" in (ROOT / "fibo_layers_backtest.py").read_text(encoding="utf-8")


# ------------------------------------------------------------------ groups
class TestGroupDetection:
    def test_every_group_has_two_distinct_extremes(self, ha):
        ho, hh, hl, hc = ha
        gs = E.groups(ho, hh, hl, hc)
        assert len(gs) > 1000
        assert all(abs(g["p0"] - g["p100"]) > 0 for g in gs)

    def test_anchors_follow_the_rule(self, ha):
        """Bullish group -> 0.0 on the group's high. Bearish -> 0.0 on its low."""
        ho, hh, hl, hc = ha
        for g in E.groups(ho, hh, hl, hc)[:200]:
            seg = slice(g["i_start"], g["i_end"] + 1)
            if g["bull"]:
                assert g["p0"] == pytest.approx(hh[seg].max())
                assert g["p100"] == pytest.approx(hl[seg].min())
            else:
                assert g["p0"] == pytest.approx(hl[seg].min())
                assert g["p100"] == pytest.approx(hh[seg].max())

    def test_fade_direction_matches_the_colour(self, ha):
        """Bullish group is SOLD, bearish is BOUGHT."""
        ho, hh, hl, hc = ha
        for g in E.groups(ho, hh, hl, hc)[:200]:
            assert (g["p0"] > g["p100"]) == g["bull"]

    def test_group_walk_terminates_and_advances(self, ha):
        """A regression guard: an inverted walk used to loop forever."""
        ho, hh, hl, hc = ha
        gs = E.groups(ho, hh, hl, hc)
        assert len(gs) > 0
        ends = [g["i_end"] for g in gs]
        assert ends == sorted(ends, reverse=True), "groups must be found newest-first"
        assert len(set(ends)) == len(ends), "each index visited once"

    def test_study_sees_long_groups(self, ha):
        """E.groups used to carry a hard-coded MAX_GROUP=12, so it reported
        "longest group = 12" while the live bot was trading a 16-candle one -
        which sent me hunting a data bug that did not exist."""
        ho, hh, hl, hc = ha
        assert E.MAX_GROUP >= 30, E.MAX_GROUP
        assert max(g["len"] for g in E.groups(ho, hh, hl, hc)) > 12

    def test_the_two_group_finders_agree(self, ha):
        """E.groups and F.groups must see the same COMPLETED runs once their two
        documented differences are accounted for:

          * F also yields the run still forming at the end (colour not flipped)
          * E walks backwards and stops once the group END passes LOOKFORWARD,
            F scans forwards from bar 0

        A silent length cap in either one makes their studies disagree, which is
        exactly what MAX_GROUP=12 did.
        """
        ho, hh, hl, hc = ha
        bull = hc >= ho
        n = len(hc)
        a = {(g["i_start"], g["i_end"]) for g in E.groups(ho, hh, hl, hc)}
        b = {(i, j) for i, j, _ in F.groups(ho, hh, hl, hc, 1, E.MAX_GROUP)
             if j + 1 < n and bull[j + 1] != bull[i] and j > E.LOOKFORWARD}
        assert a == b, "group finders disagree on %d runs" % len(a ^ b)
        assert max(j for _, j in a) > 12, "no long group in the sample"


# ------------------------------------------------------------------ no lookahead
class TestNoLookahead:
    def test_entry_cannot_fill_inside_the_group_bar(self):
        """We only know the leg once the group closes."""
        src = (ROOT / "fibo_edge_study.py").read_text(encoding="utf-8")
        assert 'range(g["i_end"] + 1' in src
        assert "never have acted on" in src

    def test_exit_cannot_book_on_the_entry_bar(self):
        src = (ROOT / "fibo_edge_study.py").read_text(encoding="utf-8")
        assert "range(i_entry + 1" in src

    def test_prefixed_run_matches_on_resolved_trades(self, bars, ha, runs):
        """Truncating history must not change trades that already finished."""
        ho, hh, hl, hc = ha
        n = len(hc)
        full = runs(lo_len=1, hi_len=3, min_ext_atr=0.0)
        cut = runs(lo_len=1, hi_len=3, min_ext_atr=0.0, end=n - 500)
        safe_f = [t for t in full if t.i_exit < n - 700]
        safe_c = [t for t in cut if t.i_exit < n - 700]
        assert len(safe_f) == len(safe_c) > 0
        for a, b in zip(safe_f, safe_c):
            assert a.i_entry == b.i_entry and a.reason == b.reason


# ------------------------------------------------------------------ edge
class TestEdgeStudy:
    def test_base_rate_matches_the_claim(self, bars, ha):
        """The teacher reckons the market mostly reaches 161.8."""
        ho, hh, hl, hc = ha
        outs = [E.reached_1618(g, hh, hl, hc) for g in E.groups(ho, hh, hl, hc)]
        outs = [o for o in outs if o]
        hit = np.mean([o[0] for o in outs])
        assert 0.50 < hit < 0.70, "hit rate moved: %.3f" % hit

    def test_win_rate_decays_as_groups_lengthen(self, bars, ha, runs):
        """The finding that drove hi_len 12 -> 3. Monotonic, no exceptions."""
        prev = 101.0
        seen = []
        for L in (1, 2, 3, 6, 12):
            w = stats(runs(lo_len=L, hi_len=L, min_ext_atr=0.0))["win"]
            seen.append(round(w, 1))
            assert w <= prev + 1e-9, "win rate rose from %d to %d candles: %s" % (L, L, seen)
            prev = w

    def test_short_groups_beat_the_old_config(self, bars, ha, runs):
        old = stats(runs(lo_len=1, hi_len=12, min_ext_atr=1.0))
        new = stats(runs(lo_len=1, hi_len=3, min_ext_atr=0.0))
        assert new["win"] > old["win"]
        assert new["pf"] > old["pf"]
        assert new["er"] > old["er"]


# ------------------------------------------------------------------ feed
class TestFeedWatchdog:
    def test_healthy_feed_short_circuits(self):
        if not LIVE:
            pytest.skip("MetaTrader 5 not answering")
        S._blind = 0
        assert S.feed_ok() is True
        assert S._blind == 0

    def test_reinitialises_on_the_first_miss(self):
        src = inspect.getsource(S.feed_ok)
        assert "_blind == 1 or _blind % 3 == 0" in src
        assert "re-initialising MT5" in src

    def test_escalates_instead_of_spinning(self):
        assert "_blind >= 9" in inspect.getsource(S.feed_ok)

    def test_cycle_never_bails_with_a_bare_no_bars(self):
        assert 'log("  no bars")' not in inspect.getsource(S.cycle)
        assert "if not feed_ok()" in inspect.getsource(S.cycle)

    def test_log_can_be_redirected_for_dry_runs(self):
        assert "FIBOSCALPER_LOGDIR" in (ROOT / "fiboscalper.py").read_text(encoding="utf-8")

    def test_a_dry_run_never_writes_the_live_log(self):
        """Verifier noise read back as the real bot skipping entries - the exact
        confusion that made it look like the bot was refusing every setup."""
        src = inspect.getsource(S.main)
        assert 'if a.dry_run:' in src and 'LOGDIR.name + "_dry"' in src
        assert src.index('LOGDIR.name + "_dry"') < src.index("--- fiboscalper"), \
            "the redirect must happen before the first log line"

    def test_suite_does_not_touch_the_live_log(self):
        """This file must redirect logging before it imports the bot.

        Checked as "not the live log" rather than "is this exact temp dir" - a
        caller may legitimately point FIBOSCALPER_LOGDIR somewhere of its own.
        """
        me = Path(__file__).read_text(encoding="utf-8")
        env = os.environ.get("FIBOSCALPER_LOGDIR")
        assert env, "the suite must set FIBOSCALPER_LOGDIR"
        assert me.index("FIBOSCALPER_LOGDIR") < me.index("import fiboscalper"), \
            "the env var must be set before the import"
        live = (ROOT / "logs").resolve()
        assert S.LOGDIR.resolve() != live, "the suite is logging to the LIVE log"
        assert live not in S.LOGDIR.resolve().parents, \
            "the suite is logging inside %s" % live
