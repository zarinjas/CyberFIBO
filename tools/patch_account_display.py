#!/usr/bin/env python3
"""Tambah identiti akaun MT5 pada paparan Telegram,
   + amaran jelas apabila state adalah TIRUAN (bukan akaun sebenar).

Mengubah 3 fail dalam repo:
  bot/traderbridge.py   pub_account terima `account=`
  bot/fiboscalper.py    hantar login/server/nama/currency/leverage
  control/traderctl     papar baris akaun + amaran TIRUAN

Guna: python tools/patch_account_display.py   (dari akar repo)
"""
import ast
import pathlib

OK = []

# ---------------------------------------------------------------- traderbridge
p = pathlib.Path("bot/traderbridge.py")
s = p.read_text(encoding="utf-8")
s = s.replace(
    '''def pub_account(balance, equity, floating, positions, arm=None, next_event="",
                paused=False, floating_arm=0.0):''',
    '''def pub_account(balance, equity, floating, positions, arm=None, next_event="",
                paused=False, floating_arm=0.0, account=None):''')
s = s.replace(
    '''    d["floating"] = round(sum(float(p.get("profit") or 0) for p in d["positions"]), 2)
    _save(d)''',
    '''    d["floating"] = round(sum(float(p.get("profit") or 0) for p in d["positions"]), 2)
    if account:
        # identiti akaun - supaya paparan Telegram tidak boleh dikelirukan
        d["account"] = dict(
            login=account.get("login"),
            server=account.get("server"),
            name=account.get("name") or "",
            currency=account.get("currency") or "",
            leverage=account.get("leverage"),
            demo=(account.get("trade_mode") == 0) if "trade_mode" in account else None,
        )
    _save(d)''')
ast.parse(s)
p.write_text(s, encoding="utf-8", newline="\n")
OK.append("bot/traderbridge.py")

# ---------------------------------------------------------------- fiboscalper
p = pathlib.Path("bot/fiboscalper.py")
s = p.read_text(encoding="utf-8")
anchor = '''            _tb.pub_account(acct.balance, acct.equity,
                            sum(r["profit"] for r in rows), rows, arm=os.environ.get("FIBOSCALPER_TAG", "?"))'''
assert anchor in s, "anchor pub_account tidak dijumpai"
s = s.replace(anchor, '''            _tb.pub_account(
                acct.balance, acct.equity, sum(r["profit"] for r in rows), rows,
                arm=os.environ.get("FIBOSCALPER_TAG", "?"),
                account=dict(login=acct.login, server=acct.server, name=acct.name,
                             currency=acct.currency, leverage=acct.leverage,
                             trade_mode=getattr(acct, "trade_mode", None)))''')
ast.parse(s)
p.write_text(s, encoding="utf-8", newline="\n")
OK.append("bot/fiboscalper.py")

# ---------------------------------------------------------------- traderctl
p = pathlib.Path("control/traderctl")
s = p.read_text(encoding="utf-8")

# helper: baris identiti akaun
s = s.replace(
    '''def cmd_status():''',
    '''def acct_line(d):
    """Satu baris identiti akaun, atau amaran kalau state tiruan."""
    a = d.get("account") or {}
    if d.get("synthetic"):
        return ("  ⚠️  DATA TIRUAN - BUKAN AKAUN SEBENAR\\n"
                "  akaun    : (tiada - MT5 belum disambung)")
    if not a.get("login"):
        return "  akaun    : (belum dilaporkan oleh bot)"
    tm = a.get("demo")
    kind = {True: "DEMO", False: "LIVE", None: "?"}[tm] if tm in (True, False, None) else "?"
    return ("  akaun    : %s @ %s  (%s) [%s]\\n"
            "  modal    : %s %s  leverage 1:%s"
            % (a.get("login"), a.get("server") or "?", a.get("name") or "?",
               kind, "", a.get("currency") or "", a.get("leverage") or "?"))


def cmd_status():''')

# paparkan dalam status
s = s.replace(
    '''    print("  keadaan  : %s" % ("⏸ PAUSE" if c.get("paused") else "▶️ JALAN"))
    print("  balance  : %s USC" % d.get("balance"))''',
    '''    print("  keadaan  : %s" % ("⏸ PAUSE" if c.get("paused") else "▶️ JALAN"))
    print(acct_line(d))
    if d.get("synthetic"):
        print("  sumber   : traderctl sim (bukan broker)")
    print("  balance  : %s USC" % d.get("balance"))''')

# paparkan dalam trades juga
s = s.replace(
    '''    ps = d.get("positions", [])
    if not ps:
        print("  TIADA posisi terbuka")
        return''',
    '''    print(acct_line(d))
    ps = d.get("positions", [])
    if not ps:
        print("\\n  TIADA posisi terbuka")
        return''')

# sim harus tunjuk identiti yang jelas TIRUAN
s = s.replace(
    '''        "synthetic": True,
        "updated": now,''',
    '''        "synthetic": True,
        "account": {"login": "SIM-0000000", "server": "SIMULASI", "name": "DATA UJIAN",
                    "currency": "USC", "leverage": 500, "demo": True},
        "updated": now,''')

ast.parse(s)
p.write_text(s, encoding="utf-8", newline="\n")
OK.append("control/traderctl")

print("dipatch:")
for f in OK:
    print("  ✓", f)
