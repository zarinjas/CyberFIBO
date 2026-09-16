#!/usr/bin/env python3
"""MT5 login helper. Credentials arrive on STDIN - never in argv or env, so
nothing shows up in `ps`. Never prints the password.
stdin: line1=server  line2=login  line3=password
"""
import json
import sys

import MetaTrader5 as mt5

raw = sys.stdin.read().split("\n")
server = raw[0].strip() if len(raw) > 0 else ""
login = raw[1].strip() if len(raw) > 1 else ""
pw = raw[2].rstrip("\n") if len(raw) > 2 else ""

out = {"server": server, "login": login, "login_ok": False, "error": None}

if not (server and login and pw):
    out["error"] = "input tidak lengkap (perlu: server, login, password)"
else:
    if not mt5.initialize():
        out["error"] = "initialize gagal: %s" % (mt5.last_error(),)
    else:
        try:
            ok = mt5.login(int(login), password=pw, server=server)
        except Exception as e:
            ok = False
            out["error"] = "%s: %s" % (type(e).__name__, e)
        out["login_ok"] = bool(ok)
        if not ok and not out["error"]:
            out["error"] = str(mt5.last_error())
        ai = mt5.account_info()
        if ai is not None:
            out["account"] = {"login": ai.login, "server": ai.server,
                              "balance": ai.balance, "equity": ai.equity,
                              "currency": ai.currency, "leverage": ai.leverage,
                              "name": ai.name}
        ti = mt5.terminal_info()
        if ti is not None:
            out["terminal_connected"] = bool(getattr(ti, "connected", False))
        mt5.shutdown()
del pw  # drop it from this process's memory on the way out

print("MT5LOGIN " + json.dumps(out))
