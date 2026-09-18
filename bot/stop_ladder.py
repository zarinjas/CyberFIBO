"""Stop Ladder — PELAKSANAAN DEMO (semak EKSEKUSI sahaja).

Bot ini melaksanakan RULE 1-4 pada akaun demo untuk mengesahkan MEKANIK:
slippage stop order, kelewatan, ketepatan paras, dan pemadaman pending.

Ia BUKAN ujian edge. Keuntungan/kerugian demo tidak membuktikan apa-apa
tentang kelebihan strategi; yang dibaca ialah log eksekusi.

RULE (verbatim daripada spec pengguna):
  1 START  : tiada posisi & tiada pending -> BUY <base> pasaran, terus RULE 2
  2 SETUP  : E = harga isian, L = lot, N = 2L
             BUY  pos SL E-4 TP E+6 ; BUY STOP N @E+6 (SL E+2, TP E+12)
                                      SELL STOP N @E-4 (SL E,   TP E-10)
             SELL pos SL E+4 TP E-6 ; SELL STOP N @E-6 (SL E-2, TP E-12)
                                      BUY STOP N @E+4 (SL E,   TP E+10)
             N > MAX_LOT -> tiada pending
  3 FILL   : padam pending lain ; biar posisi lama (SL/TP sendiri tutup) ;
             posisi baharu jadi current -> RULE 2
  4 END    : posisi tutup tanpa posisi baharu -> padam semua pending, stop, beritahu

Guna: python stop_ladder.py [--doubling 0|1] [--base-lot 0.01] [--dry]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import json                                    # noqa: E402
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import MetaTrader5 as mt5                      # noqa: E402

import mt5lib                                  # noqa: E402
import strategies                              # noqa: E402
import traderbridge as bridge                  # noqa: E402

SYM = "XAUUSDc"
TP, SL = 6.00, 4.00            # harga, bukan point (1 pip = 0.10 pada spec)
LEVEL_CAP = 7
ROOT = Path(os.environ.get("TRADER_ROOT", "C:/cyberfibo"))
STATE = ROOT / "state" / "ladder.json"
EXECLOG = ROOT / "logs" / "ladder-exec.jsonl"


def log_exec(rec: dict) -> None:
    """Rekod eksekusi: satu baris JSON setiap peristiwa yang boleh diaudit."""
    rec["ts"] = int(time.time())
    try:
        EXECLOG.parent.mkdir(parents=True, exist_ok=True)
        with EXECLOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except Exception:
        pass


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(d: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2, default=str), encoding="utf-8")
    tmp.replace(STATE)


def info():
    return mt5.account_info()


def pip(price: float) -> float:
    """1 pip = 0.10 harga (spec pengguna)."""
    return round(price / 0.10, 1)


class Ladder:
    def __init__(self, magic: int, base_lot: float, max_lot: float, doubling: bool,
                 dry: bool, level_cap: int = 0):
        self.magic = magic
        self.base_lot = base_lot
        self.max_lot = max_lot
        self.doubling = doubling
        self.dry = dry
        self.level_cap = level_cap

    # ---------------------------------------------------------------- helpers
    def positions(self):
        return [p for p in (mt5.positions_get(symbol=SYM) or []) if p.magic == self.magic]

    def orders(self):
        return [o for o in (mt5.orders_get(symbol=SYM) or []) if o.magic == self.magic]

    def send(self, req: dict, kind: str, level: float = None):
        if self.dry:
            print("  DRY %-12s %s" % (kind, req.get("price") or "market"))
            return None
        t0 = time.time()
        r = mt5.order_send(req)
        dt_ms = (time.time() - t0) * 1000.0
        rec = dict(kind=kind, retcode=getattr(r, "retcode", None),
                   comment=getattr(r, "comment", ""), ms=round(dt_ms, 1))
        if level is not None:
            fill = getattr(r, "price", None)
            rec["level"] = round(level, 2)
            rec["fill"] = round(fill, 2) if fill else None
            if fill:
                rec["slippage_pt"] = round((fill - level) / 0.01, 1)
                rec["accuracy_pip"] = round(abs(fill - level) / 0.10, 2)
        log_exec(rec)
        return r

    def market_open(self, direction: int, lot: float):
        tick = mt5.symbol_info_tick(SYM)
        px = tick.ask if direction > 0 else tick.bid
        req = dict(action=mt5.TRADE_ACTION_DEAL, symbol=SYM, volume=lot,
                   type=mt5.ORDER_TYPE_BUY if direction > 0 else mt5.ORDER_TYPE_SELL,
                   price=px, deviation=20, magic=self.magic, comment="ladder-open",
                   type_time=mt5.ORDER_TIME_GTC, type_filling=mt5.ORDER_FILLING_FOK)
        return self.send(req, "market_open", px)

    def spread_pts(self) -> float:
        """Spread semasa dalam point. 0 kalau tiada tick."""
        t = mt5.symbol_info_tick(SYM)
        if not t or t.ask <= 0 or t.bid <= 0:
            return 0.0
        return round((t.ask - t.bid) / 0.01, 1)

    def place_pendings(self, direction: int, entry: float, lot: float, level: int = 1):
        """RULE 2. Pulangkan senarai harga yang BERJAYA dipasang.

        Spread: harga `entry` yang kita guna ialah harga ISIAN (BID untuk SELL,
        ASK untuk BUY). Pending baharu mesti dipasang dari harga yang BETUL
        untuk arahnya, jika tidak kedua-dua pending boleh terisi serentak bila
        spread melebar — dan kita dapat dua entry bertentangan pada masa sama.
        Offset kecil ke sisi selamat dipakai di sini.
        """
        if self.level_cap and level >= self.level_cap:
            log_exec(dict(kind="level_cap", level=level, cap=self.level_cap))
            return []
        n = 2 * lot
        if round(n, 2) > self.max_lot + 1e-9:
            log_exec(dict(kind="no_pending_cap", lot=lot, n=round(n, 2), max_lot=self.max_lot))
            return []
        sp = self.spread_pts()
        if direction > 0:
            # BUY: masuk di ASK. BUY_STOP mesti >= ask; SELL_STOP jauh di bawah.
            specs = [(mt5.ORDER_TYPE_BUY_STOP, entry + TP, entry + 2.00, entry + 12.00),
                     (mt5.ORDER_TYPE_SELL_STOP, entry - SL, entry, entry - 10.00)]
        else:
            specs = [(mt5.ORDER_TYPE_SELL_STOP, entry - TP, entry - 2.00, entry - 12.00),
                     (mt5.ORDER_TYPE_BUY_STOP, entry + SL, entry, entry + 10.00)]
        log_exec(dict(kind="pending_spread", direction=direction, spread_pts=sp,
                      gap=round(TP + SL, 2), note="jarak dua pending = TP+SL"))
        made = []
        for otype, px, sl, tp in specs:
            req = dict(action=mt5.TRADE_ACTION_PENDING, symbol=SYM, volume=round(n, 2),
                       type=otype, price=round(px, 2), sl=round(sl, 2), tp=round(tp, 2),
                       magic=self.magic, comment="ladder-pend", type_time=mt5.ORDER_TIME_GTC,
                       type_filling=mt5.ORDER_FILLING_FOK)
            r = self.send(req, "place_pending", px)
            if r is not None and getattr(r, "retcode", 0) == mt5.TRADE_RETCODE_DONE:
                made.append(px)
        return made

    def delete_others(self, keep_ticket=None):
        """RULE 3.1 / RULE 4: padam pending yang tinggal. Sahkan selepas padam."""
        left = [o for o in self.orders() if o.ticket != keep_ticket]
        for o in left:
            req = dict(action=mt5.TRADE_ACTION_REMOVE, order=o.ticket)
            self.send(req, "delete_pending")
        time.sleep(0.4)
        after = [o for o in self.orders() if o.ticket != keep_ticket]
        log_exec(dict(kind="delete_check", asked=len(left), remaining=len(after),
                      ok=(len(after) == 0)))
        return len(after) == 0


REQ = ROOT / "state" / "request.json"


def _apply_request(lad) -> None:
    """Had lot daripada butang Telegram (ROPE LOT): rope_min / rope_max.

    `lad` ialah objek Ladder (dihantar secara eksplisit — jangan guna
    pemboleh ubah global, ia belum wujud semasa poll pertama).

    Ladder gandakan lot setiap aras (N = 2L), jadi lot TIDAK dikira dari
    % risiko. Had bawah = lot mula, had atas = siling ganda.
    """
    try:
        if not REQ.exists():
            return
        q = json.loads(REQ.read_text())
        REQ.unlink()
    except Exception:
        return
    try:
        mn, mx = q.get("rope_min"), q.get("rope_max")
        # Sahkan KEDUA-DUA dahulu. Kalau salah satu cacat, tolak
        # permintaan itu SEPENUHNYA — jangan pakai separuh, nanti had
        # jadi bercanggah (min 0.01 dengan max 0.64 sedangkan diminta 0.16).
        vmin = vmax = None
        if mn is not None:
            vmin = round(float(mn), 2)
            if not (0.01 <= vmin <= 5.0):
                log_exec(dict(kind="rope_reject", which="min", value=str(mn)))
                return
        if mx is not None:
            vmax = round(float(mx), 2)
            if not (0.01 <= vmax <= 5.0):
                log_exec(dict(kind="rope_reject", which="max", value=str(mx)))
                return
        if vmin is not None and vmax is not None and vmin > vmax:
            log_exec(dict(kind="rope_reject", which="min>max",
                          min=vmin, max=vmax))
            return
        if vmin is not None:
            lad.base_lot = vmin
            log_exec(dict(kind="control_rope_min", lot=vmin))
            print("  control: rope MINIMUM lot -> %.2f" % vmin, flush=True)
        if vmax is not None:
            lad.max_lot = vmax
            log_exec(dict(kind="control_rope_max", lot=vmax))
            print("  control: rope MAKSIMUM lot -> %.2f" % vmax, flush=True)
    except (TypeError, ValueError):
        pass


def trend_ok(threshold: float):
    """Tapis trend: M5 EMA20/50, kuat = |EMAf-EMAs|/ATR. Pulangkan (ok, arah)."""
    r = mt5.copy_rates_from_pos(SYM, mt5.TIMEFRAME_M5, 0, 400)
    if r is None or len(r) < 220:
        return False, 0
    c = r["close"].astype(float)
    a = 2.0 / 21.0
    ef = c[0]
    for x in c[1:]:
        ef = a * x + (1 - a) * ef
    es = c[0]
    a2 = 2.0 / 51.0
    for x in c[1:]:
        es = a2 * x + (1 - a2) * es
    # ATR(14) ringkas pada bar terakhir
    h, l, cl = r["high"].astype(float), r["low"].astype(float), c
    tr = [max(h[i] - l[i], abs(h[i] - cl[i - 1]), abs(l[i] - cl[i - 1])) for i in range(-14, 0)]
    atr = sum(tr) / len(tr) or 1e-9
    d = 1 if ef > es else -1
    return (abs(ef - es) / atr) >= threshold, d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--doubling", type=int, default=1, help="1 = versi B, 0 = versi A")
    ap.add_argument("--base-lot", type=float, default=0.01)
    ap.add_argument("--max-lot", type=float, default=0.64)
    ap.add_argument("--magic", type=int, default=9200)
    ap.add_argument("--threshold", type=float, default=0.4)
    ap.add_argument("--poll", type=int, default=3)
    ap.add_argument("--level-cap", type=int, default=0,
                    help="had aras sebelum berhenti (0 = tiada had); RULE 4 diuji bila ditetapkan")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    if not mt5lib.connect():
        sys.exit("MT5 gagal disambung")
    mt5.symbol_select(SYM, True)

    # RULE 1 spec kata "tiada posisi & tiada pending" pada XAUUSD. Pada akaun yang
    # dikongsi dengan FIBO_V1, itu akan menyekat ladder hampir selamanya. Jadi
    # RULE 1 diskopkan kepada buku ladder sendiri (magic), dan setiap kali ia
    # TERSEKAT oleh posisi bukan-ladder, itu DILOG supaya sisihan ini kelihatan.
    acc_check = mt5.account_info()
    is_demo = (getattr(acc_check, "trade_mode", None) == 0)
    strategies.guard("STOP_LADDER", is_demo=is_demo)
    L = Ladder(a.magic, a.base_lot, a.max_lot, bool(a.doubling), a.dry, a.level_cap)
    acc = info()
    bridge.event("info", "Stop Ladder DEMO mula | versi %s | lot %.2f-%.2f | magic %d | %s %.2f %s"
                 % ("B(berganda)" if a.doubling else "A(tetap)", a.base_lot, a.max_lot, a.magic,
                    acc.login, acc.balance, acc.currency))
    print("  mula: versi %s · lot %.2f-%.2f · magic %d"
          % ("B" if a.doubling else "A", a.base_lot, a.max_lot, a.magic))

    while True:
        try:
            _apply_request(L)
            pos, ords = L.positions(), L.orders()
            st = load_state()

            other = [p for p in (mt5.positions_get(symbol=SYM) or []) if p.magic != L.magic]
            if not pos and not ords and other:
                log_exec(dict(kind="blocked_by_other_book", n=len(other),
                              magics=sorted({p.magic for p in other})))
            # SWEEP: TP/SL sudah kena tetapi pending masih tertinggal.
            # Spec Hafiz: "setiap kali hit TP, SL dan PO tu kena delete".
            # Disahkan dua poll berturut-turut supaya saat peralihan
            # (posisi tutup, pending belum isi) tidak memadam pending sah.
            if not pos and ords:
                n = int(st.get("orphan_n", 0)) + 1
                st["orphan_n"] = n
                save_state(st)
                log_exec(dict(kind="orphan_seen", n=n, pendings=len(ords)))
                if n >= 2:
                    log_exec(dict(kind="sweep_tp_sl", pendings=len(ords),
                                  note="tiada posisi buku-ladder -> padam semua pending"))
                    L.delete_others()
                    save_state(dict(active=False, ended=True))
                    bridge.event("exit", "LADDER: tiada posisi, %d pending dipadam (sweep)"
                                 % len(ords))
                time.sleep(a.poll)
                continue
            if pos:
                st["orphan_n"] = 0
                save_state(st)

            if not pos and not ords:
                ok, d = trend_ok(a.threshold)
                if not ok:
                    time.sleep(a.poll)
                    continue
                if st.get("ended"):
                    print("  END sebelum ini — menunggu (RULE 4: perlu kelulusan Hafiz)")
                    time.sleep(a.poll * 5)
                    continue
                r = L.market_open(d, a.base_lot)
                if r is None or getattr(r, "retcode", 0) != mt5.TRADE_RETCODE_DONE:
                    time.sleep(a.poll)
                    continue
                entry = getattr(r, "price", None) or (mt5.symbol_info_tick(SYM).ask if d > 0
                                                      else mt5.symbol_info_tick(SYM).bid)
                save_state(dict(active=True, dir=d, level=1, lot=a.base_lot,
                                entry=entry, ticket=getattr(r, "order", 0), started=int(time.time())))
                pos = L.positions()
                if pos:
                    p = pos[0]
                    mt5.order_send(dict(action=mt5.TRADE_ACTION_SLTP, position=p.ticket, symbol=SYM,
                                        sl=round(p.price_open - SL, 2) if d > 0 else round(p.price_open + SL, 2),
                                        tp=round(p.price_open + TP, 2) if d > 0 else round(p.price_open - TP, 2)))
                    L.place_pendings(d, p.price_open, a.base_lot, 1)
                continue

            if not st.get("active"):
                save_state(dict(active=True, dir=1 if pos else 0, ticket=pos[0].ticket if pos else 0))
                st = load_state()

            cur = [p for p in pos if p.ticket == st.get("ticket")]
            fresh = [p for p in pos if p.ticket != st.get("ticket")]

            if fresh:
                np_ = fresh[0]
                nppx = float(np_.price_open)
                log_exec(dict(kind="pending_filled", ticket=np_.ticket, price=round(nppx, 2),
                              volume=np_.volume, requested=st.get("entry")))
                if st.get("entry"):
                    log_exec(dict(kind="fill_slippage", level=round(float(st["entry"]), 2),
                                  fill=round(nppx, 2),
                                  slippage_pt=round((nppx - float(st["entry"])) / 0.01, 1),
                                  accuracy_pip=round(abs(nppx - float(st["entry"])) / 0.10, 2)))
                L.delete_others()
                lvl = int(st.get("level", 1)) + 1
                lot = min(round(np_.volume, 2), L.max_lot)
                save_state(dict(active=True, dir=1 if np_.type == mt5.POSITION_TYPE_BUY else -1,
                                level=lvl, lot=lot, entry=nppx, ticket=np_.ticket))
                L.place_pendings(1 if np_.type == mt5.POSITION_TYPE_BUY else -1, nppx, lot, lvl)
                bridge.event("entry", "LADDER aras %d | lot %.2f | isian %.2f (paras %.2f) | %s"
                             % (lvl, lot, nppx, st.get("entry") or 0, SYM))
            elif cur and not ords:
                # RULE 2 dipulihkan: pending hilang (bot restart) -> pasang semula
                p = cur[0]
                d_cur = 1 if p.type == mt5.POSITION_TYPE_BUY else -1
                log_exec(dict(kind="pendings_restored", ticket=p.ticket,
                              level=st.get("level"), dir=d_cur))
                L.place_pendings(d_cur, float(p.price_open), float(p.volume),
                                 int(st.get("level") or 1))
            elif not cur:
                log_exec(dict(kind="tp_or_sl_hit", level=st.get("level"),
                              note="posisi tutup -> padam semua pending (RULE 4)"))
                L.delete_others()
                save_state(dict(active=False, ended=True))
                bridge.event("exit", "LADDER TAMAT (RULE 4) — %d aras. Pending dipadam. "
                                     "Perlu kelulusan Hafiz untuk mula semula." % st.get("level", 0))
        except Exception as ex:
            # JANGAN telan senyap: ralat mesti kelihatan dalam stdout DAN log,
            # sebab ralat senyap dalam try/except pernah melumpuhkan kawalan
            # Telegram tanpa sesiapa perasan.
            log_exec(dict(kind="error", error=str(ex)[:300]))
            print("  RALAT: %s" % str(ex)[:200], file=sys.stderr, flush=True)
        time.sleep(a.poll)


if __name__ == "__main__":
    main()
