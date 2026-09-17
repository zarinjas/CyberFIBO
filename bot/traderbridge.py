"""traderbridge - the ONLY thing the scalper does to talk to the control plane.

Runs under Windows Python inside Wine (the MetaTrader5 module is Windows-only),
so paths are Z:/opt/trading/... which Wine maps to /opt/trading on the host.

The scalper calls three functions:

    pub_arm(...)      once per arm per poll -> feeds `traderctl status/next/why`
    pub_account(...)  once per poll         -> balances + open positions
    event(kind, txt)  on every fill/exit    -> pushed to Telegram by trader-notify
    control()         once per poll         -> reads pause/skip/close_all

Nothing here decides anything. It publishes state; traderctl renders it.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

_DEF = "Z:/opt/trading" if os.name == "nt" else "/opt/trading"
ROOT = Path(os.environ.get("TRADER_ROOT", _DEF))
STATE = ROOT / "state"
ARMS = STATE / "arms.json"
CTRL = STATE / "control.json"
EVENTS = ROOT / "events.jsonl"

_seq = 0

# Setiap trade dan notifikasi membawa strategy_id, supaya posisi tidak pernah
# bercampur antara strategy. Import dilindungi: kalau registry hilang, bot lama
# masih berjalan (cuma tanpa tag).
try:
    import strategies as _st
except Exception:                                   # pragma: no cover
    _st = None


def strategy_id() -> str:
    """id strategy untuk dilampirkan. Env STRATEGY_ID menang; jika tidak, gunakan
    strategy aktif yang disimpan."""
    try:
        return os.environ.get("STRATEGY_ID") or (_st.current_id() if _st else "FIBO_V1")
    except Exception:
        return os.environ.get("STRATEGY_ID", "FIBO_V1")


def event(kind: str, text: str) -> None:
    """Append one line for trader-notify to push. kind: entry|exit|skip|error|info"""
    global _seq
    try:
        ROOT.mkdir(parents=True, exist_ok=True)
        with EVENTS.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": int(time.time()), "kind": kind,
                                "strategy_id": strategy_id(),
                                "text": text[:900], "seq": _seq}) + "\n")
        _seq += 1
    except Exception:
        pass


def control(default=None):
    """{"paused":bool,"skip":bool,"close_all":bool} written by traderctl/bot."""
    base = {"paused": False, "skip": False, "close_all": False}
    try:
        if CTRL.exists():
            base.update(json.loads(CTRL.read_text(encoding="utf-8")))
    except Exception:
        pass
    return base if default is None else {**base, **default}


def _load():
    try:
        if ARMS.exists():
            return json.loads(ARMS.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"arms": [], "positions": []}


def _save(d):
    d["updated"] = int(time.time())
    d["synthetic"] = False
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        tmp = ARMS.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=2, default=str), encoding="utf-8")
        tmp.replace(ARMS)                      # atomic: traderctl never reads half a file
    except Exception:
        pass


def pub_arm(name, tf, magic, action, reason="", group="", levels="", price="", zone="",
            state_icon="…", trades_today=None):
    d = _load()
    arms = [a for a in d.get("arms", []) if a.get("name") != name]
    arms.append(dict(name=name, tf=tf, magic=magic, action=action, reason=reason,
                     group=group, levels=levels, price=price, zone=zone,
                     state=state_icon, trades_today=trades_today,
                     strategy_id=strategy_id()))
    d["arms"] = sorted(arms, key=lambda a: str(a.get("name")))
    _save(d)


def pub_account(balance, equity, floating, positions, arm=None, next_event="",
                paused=False, floating_arm=0.0, account=None):
    """Each arm publishes only ITS OWN positions; the others are preserved by
    matching on `arm`, so N arms don't clobber each other's rows."""
    d = _load()
    sid = strategy_id()
    rows = [dict(p, strategy_id=p.get("strategy_id") or sid) for p in positions]
    if arm:
        keep = [p for p in d.get("positions", []) if p.get("arm") != arm]
    else:
        keep = []
    d.update(balance=round(float(balance), 2), equity=round(float(equity), 2),
             positions=keep + rows,
             strategy_id=sid,
             next_event=next_event, control=dict(control(), paused=paused))
    d["floating"] = round(sum(float(p.get("profit") or 0) for p in d["positions"]), 2)
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
    _save(d)
