"""Registry broker + akaun, dan penukaran akaun dari Telegram.

SENI BINA B - satu broker pada satu masa.

Sebab ia satu pada satu masa: pakej MetaTrader5 Python hanya membaca terminal
di laluan pemasangan LAZIM (disahkan: terminal kedua di folder lain pulang
-10004/-10005 walau login berjaya, walau hanya satu terminal berjalan). Jadi
kita guna SATU terminal dan tukar akaunnya dengan mt5.login().

Setiap broker membawa `method` (strategy_id dalam bot/strategies.py) dan `pair`
(simbol). Tukar broker = tukar kedua-duanya sekali.

Kata laluan disimpan dalam state/brokers.key.json dan TIDAK pernah dilog.
"""
import json
import os
from pathlib import Path

_DEF = "Z:/opt/trading" if os.name == "nt" else "/opt/trading"
ROOT = Path(os.environ.get("TRADER_ROOT", _DEF))
CUR = ROOT / "state" / "broker.json"
KEY = ROOT / "state" / "brokers.key.json"

# slug -> broker. `method` mesti wujud dalam bot/strategies.py REGISTRY.
BROKERS = {
    "tradingpro": dict(
        name="TradingPro", server="TradingProInternational-Demo", login="5008941",
        pair="XAUUSDc", method="FIBO_V1", magic=9151, currency="USC",
    ),
    "vantage": dict(
        name="Vantage", server="VantageMarkets-Demo", login="26124836",
        pair="FixedVol100", method="STOP_LADDER", magic=9300, currency="USD",
    ),
}
DEFAULT = "tradingpro"
ICON = {"tradingpro": "\U0001f7e2", "vantage": "\U0001f7e0"}


def resolve(word):
    """Slug, nama broker, atau simbol -> slug. None kalau tidak dikenali."""
    w = (word or "").strip().lower()
    if not w:
        return None
    for slug, b in BROKERS.items():
        if w in (slug, b["name"].lower(), b["pair"].lower()):
            return slug
    for slug, b in BROKERS.items():
        if w and (w in slug or w in b["name"].lower()):
            return slug
    return None


def info(slug):
    return BROKERS.get(slug, {})


def _read(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:                                  # noqa: BLE001
        return default


def current():
    """Slug broker aktif. Jatuh balik ke DEFAULT kalau fail tiada/rosak."""
    slug = _read(CUR, {}).get("slug")
    return slug if slug in BROKERS else DEFAULT


def set_current(slug):
    CUR.parent.mkdir(parents=True, exist_ok=True)
    CUR.write_text(json.dumps({"slug": slug, "when": int(__import__("time").time())},
                              indent=1), encoding="utf-8")
    return slug


def pair(slug=None):
    return info(slug or current()).get("pair", "?")


def method(slug=None):
    return info(slug or current()).get("method", "?")


def password(slug):
    """Baca kata laluan: env MT5PW_<SLUG> menang, kemudian fail kunci."""
    env = os.environ.get("MT5PW_" + slug.upper())
    if env:
        return env
    return _read(KEY, {}).get(slug)


def save_password(slug, pw):
    """Simpan kata laluan broker. Fail ini BUKAN untuk repo."""
    d = _read(KEY, {})
    d[slug] = pw
    KEY.parent.mkdir(parents=True, exist_ok=True)
    KEY.write_text(json.dumps(d, indent=1), encoding="utf-8")
    try:                                    # hadkan akses (Windows/Linux)
        os.chmod(KEY, 0o600)
    except Exception:                       # noqa: BLE001
        pass
    return True


def line(slug):
    b = BROKERS[slug]
    mark = "  <- aktif" if slug == current() else ""
    return "  %s %-12s %-16s %s · %s%s" % (
        ICON.get(slug, "\u26aa"), b["name"], b["pair"], b["server"],
        "kunci ada" if password(slug) else "tiada kunci", mark)


def listing():
    return [line(s) for s in BROKERS]
