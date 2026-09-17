"""
Shared helpers for the MT5 execution-agent toolkit.

Single source of truth for: connection, bar loading, indicator math, clock, and
report formatting. Every script in this folder imports from here - do not
re-implement EMA/ATR/ADX anywhere else.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import MetaTrader5 as mt5
import numpy as np

MT5_PATH_DEFAULT = r"C:\Program Files\MetaTrader 5\terminal64.exe"

SYMBOL = "XAUUSDc"          # the only tradeable XAU symbol (XAUUSD is close-only)
M5, M15 = mt5.TIMEFRAME_M5, mt5.TIMEFRAME_M15
MYT = ZoneInfo("Asia/Kuala_Lumpur")

OK, WARN, BAD, NA = "[OK]  ", "[WARN]", "[FAIL]", "[ -- ]"


# ---------------------------------------------------------------- connection
def connect():
    """Initialize the terminal. Returns (terminal_info, account_info).

    The terminal path is passed explicitly because a service session (Windows
    Scheduled Task running as SYSTEM) has no registry view of the user install
    and cannot discover the terminal on its own - without it initialize() fails
    with "IPC initialize failed, MetaTrader 5 x64 not found". Override with
    MT5_PATH, or omit the file to fall back to auto-discovery.
    """
    path = os.environ.get("MT5_PATH", MT5_PATH_DEFAULT)
    ok = mt5.initialize(path=path) if path and os.path.exists(path) else mt5.initialize()
    if not ok:
        raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")
    return mt5.terminal_info(), mt5.account_info()


def bars(tf: int, n: int = 400):
    """OHLCV dict of the last n bars, oldest -> newest (index -1 = forming bar)."""
    r = mt5.copy_rates_from_pos(SYMBOL, tf, 0, n)
    if r is None:
        return None
    return {k: r[k].astype(float) for k in ("time", "open", "high", "low", "close")}


# ---------------------------------------------------------------- orders
def close_position(pos, deviation: int = 30, comment: str = "hermes close"):
    """Close one position at market.

    TRADE_ACTION_DEAL *requires* `price` - omitting it makes the terminal
    reject the request with retcode 10013 'Invalid request'. Closing a SELL
    means buying at the ask; closing a BUY means selling at the bid.
    """
    t = mt5.symbol_info_tick(pos.symbol)
    if t is None:
        return None
    closing_buy = pos.type == mt5.POSITION_TYPE_SELL
    return mt5.order_send({
        "action": mt5.TRADE_ACTION_DEAL,
        "position": pos.ticket,
        "symbol": pos.symbol,
        "volume": pos.volume,
        "type": mt5.ORDER_TYPE_BUY if closing_buy else mt5.ORDER_TYPE_SELL,
        "price": t.ask if closing_buy else t.bid,
        "deviation": deviation,
        "magic": 0,
        "comment": comment,
        "type_filling": mt5.ORDER_FILLING_FOK,
    })


def close_all(deviation: int = 30, comment: str = "hermes close-all") -> list:
    """Close every open position. Returns [(ticket, retcode_or_None), ...]."""
    out = []
    for p in (mt5.positions_get() or ()):
        r = close_position(p, deviation, comment)
        out.append((p.ticket, None if r is None else r.retcode))
    return out


# ---------------------------------------------------------------- indicators
def ema(v: np.ndarray, p: int) -> np.ndarray:
    """EMA seeded with the SMA of the first p values."""
    out = np.full(v.size, np.nan)
    if v.size < p:
        return out
    k = 2.0 / (p + 1.0)
    out[p - 1] = v[:p].mean()
    for i in range(p, v.size):
        out[i] = v[i] * k + out[i - 1] * (1 - k)
    return out


def wilder(v: np.ndarray, p: int) -> np.ndarray:
    """Wilder smoothing: seed = sum of first p, then s -= s/p; s += v."""
    out = np.full(v.size, np.nan)
    if v.size < p:
        return out
    s = v[:p].sum()
    out[p - 1] = s
    for i in range(p, v.size):
        s = s - s / p + v[i]
        out[i] = s
    return out


def true_range(h, l, c) -> np.ndarray:
    pc = np.roll(c, 1)
    pc[0] = c[0]
    return np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))


def atr(h, l, c, p: int = 14) -> np.ndarray:
    """MT5 iATR equivalent (SMMA of true range)."""
    return wilder(true_range(h, l, c), p) / p


def adx(h, l, c, p: int = 14):
    """Returns (adx, plus_di, minus_di), Wilder smoothing."""
    up, dn = h[1:] - h[:-1], l[:-1] - l[1:]
    pdm = np.concatenate([[0.0], np.where((up > dn) & (up > 0), up, 0.0)])
    ndm = np.concatenate([[0.0], np.where((dn > up) & (dn > 0), dn, 0.0)])
    st = wilder(true_range(h, l, c), p)
    with np.errstate(divide="ignore", invalid="ignore"):
        pdi = 100.0 * wilder(pdm, p) / st
        ndi = 100.0 * wilder(ndm, p) / st
        dx = 100.0 * np.abs(pdi - ndi) / (pdi + ndi)
    return wilder(np.nan_to_num(dx), p) / p, pdi, ndi


def rsi(c, p: int = 14) -> np.ndarray:
    """MT5 iRSI equivalent (Wilder smoothing of up/down moves)."""
    d = np.diff(c, prepend=c[0])
    au = wilder(np.where(d > 0, d, 0.0), p)
    ad = wilder(np.where(d < 0, -d, 0.0), p)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.nan_to_num(np.where(ad > 0, 100 - 100 / (1 + au / np.maximum(ad, 1e-12)),
                                      100.0), nan=50.0)


# ---------------------------------------------------------------- clock
_OFFSET_CACHE: float | None = None


def server_offset() -> float:
    """Measured broker GMT offset in hours, from the live tick timestamp.

    Caches the last good value so a transient terminal hiccup doesn't kill a
    cycle, but raises a clear error if we never had one (rather than letting an
    AttributeError escape from deep inside order construction).
    """
    global _OFFSET_CACHE
    tick = mt5.symbol_info_tick(SYMBOL)
    if tick is not None:
        _OFFSET_CACHE = round((tick.time - time.time()) / 900.0) * 0.25
    if _OFFSET_CACHE is None:
        raise RuntimeError("server_offset: terminal returned no tick and no cached offset")
    return _OFFSET_CACHE


def now_myt() -> datetime:
    return datetime.now(MYT)


def server_dt(epoch: int) -> datetime:
    """MT5 stamps are server-local encoded as epoch -> render as UTC for display."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


# ---------------------------------------------------------------- reporting
def section(title: str) -> None:
    print("\n" + "=" * 74 + f"\n{title}\n" + "=" * 74)


def check(name: str, ok, detail: str = "") -> None:
    """ok may be None (n/a) or any truthy/falsy - numpy bools are coerced."""
    tag = NA if ok is None else (OK if bool(ok) else BAD)
    print(f"  {tag} {name}" + (f"  {detail}" if detail else ""))


def dump(label: str, obj, keys=None) -> None:
    """Print an MT5 namedtuple as aligned key/value rows."""
    section(label)
    if obj is None:
        print("  <None>")
        return
    d = obj._asdict() if hasattr(obj, "_asdict") else obj
    for k, v in d.items():
        if keys and k not in keys:
            continue
        if isinstance(v, float):
            v = f"{v:,.5f}".rstrip("0").rstrip(".")
        print(f"  {k:24} {v}")
