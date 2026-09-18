"""Pekerja tukar broker/akaun - dijalankan sekali oleh traderctl.

SENI BINA B: satu terminal MT5, satu akaun pada satu masa. Tukar broker =
keluar akaun lama, masuk akaun baharu pada terminal yang SAMA.

Aliran:
  1. baca state/switch.json  {"slug": "<sasaran>"}
  2. GUARD FLAT - tolak kalau ada posisi terbuka (elak nukar akaun separuh jalan)
  3. hentikan task dagangan
  4. mt5.login(login, kata laluan, server)
  5. sahkan account_info().login == sasaran
  6. brokers.set_current(slug)
  7. mula runner kaedah broker baharu (strategies.switch_runner)
  8. tulis state/switch_result.json untuk traderctl lapor

Guna: python mt5_switch.py            (baca fail permintaan)
      python mt5_switch.py vantage    (slug terus)
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brokers as B                                          # noqa: E402
import strategies as S                                       # noqa: E402

REQ = B.ROOT / "state" / "switch.json"
OUT = B.ROOT / "state" / "switch_result.json"

# Task dagangan yang mesti berhenti sebelum tukar akaun.
TRADE_TASKS = ("CyberFIBO-ScalperX", "CyberFIBO-Ladder", "CyberFIBO-ScalperW")


def _task(cmd, name):
    import subprocess
    try:
        r = subprocess.run(["schtasks", "/" + cmd, "/tn", name],
                           capture_output=True, text=True, timeout=60)
        return r.returncode == 0
    except Exception:                                        # noqa: BLE001
        return False


def stop_trading():
    if __import__("os").name != "nt":
        return ["bukan Windows: lompat"]
    return ["%s:%s" % (t, "berhenti" if _task("end", t) else "sudah mati")
            for t in TRADE_TASKS]


def connect():
    """Sambung ke terminal MT5. Pulangkan (mt5, err)."""
    try:
        import MetaTrader5 as mt5
    except Exception as e:                                   # noqa: BLE001
        return None, "MetaTrader5 tiada: %s" % e
    import os
    p = os.environ.get("MT5_PATH")
    ok = mt5.initialize(path=p) if p and os.path.exists(p) else mt5.initialize()
    if not ok:
        return None, "initialize gagal: %s" % (mt5.last_error(),)
    return mt5, None


def switch(slug):
    """Tukar ke broker `slug`. Pulangkan (ok, mesej, butiran)."""
    b = B.info(slug)
    if not b:
        return False, "broker tidak dikenali: %s" % slug, {}
    pw = B.password(slug)
    if not pw:
        return False, ("broker %s tiada kata laluan tersimpan.\n"
                       "Hantar: /brokerkey %s <kata_laluan>" % (b["name"], slug)), {}

    mt5, err = connect()
    if err:
        return False, err, {}
    detail = {}
    try:
        # ---- GUARD FLAT -------------------------------------------------
        pos = mt5.positions_get() or []
        if pos:
            tot = sum(p.volume for p in pos)
            return False, ("ada %d posisi terbuka (%.2f lot) - TUTUP dahulu "
                           "sebelum tukar broker.\nGuna butang CLOSE ALL."
                           % (len(pos), tot)), {"posisi": len(pos)}
        detail["flat"] = True

        # ---- berhenti dagang -------------------------------------------
        detail["stop"] = stop_trading()
        time.sleep(3)

        # ---- tukar akaun ------------------------------------------------
        ok = mt5.login(int(b["login"]), password=pw, server=b["server"])
        if not ok:
            return False, "mt5.login gagal: %s" % (mt5.last_error(),), detail
        a = mt5.account_info()
        if not a or str(a.login) != str(b["login"]):
            return False, ("login tidak padan: dapat %s, jangka %s"
                           % (getattr(a, "login", "?"), b["login"])), detail
        detail.update(akaun=a.login, server=a.server, baki=a.balance,
                      mata_wang=a.currency)

        # ---- rekod + mula runner ---------------------------------------
        B.set_current(slug)
        ok2, msg2 = S.switch_runner(b["method"])
        detail["runner"] = msg2
        if not ok2:
            return False, "akaun bertukar tetapi runner gagal: %s" % msg2, detail
    finally:
        try:
            mt5.shutdown()
        except Exception:                                    # noqa: BLE001
            pass
    return True, "bertukar ke %s (%s · %s)" % (b["name"], b["pair"], b["server"]), detail


def main():
    slug = sys.argv[1] if len(sys.argv) > 1 else None
    if not slug:
        try:
            slug = json.loads(REQ.read_text(encoding="utf-8")).get("slug")
        except Exception:                                    # noqa: BLE001
            slug = None
        try:
            REQ.unlink()
        except Exception:                                    # noqa: BLE001
            pass
    if not slug:
        print("  tiada permintaan tukar broker")
        return
    slug = B.resolve(slug) or slug
    ok, msg, detail = switch(slug)
    out = dict(ok=ok, slug=slug, msg=msg, detail=detail, when=int(time.time()))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print("  %s %s" % ("OK " if ok else "GAGAL", msg))


if __name__ == "__main__":
    main()
