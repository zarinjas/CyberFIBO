"""Registry strategy + strategy aktif yang kekal.

Menambah strategy baharu = menambah SATU entri dalam REGISTRY. Enjin dan
traderctl tidak perlu diubah.

Status:
  ACTIVE        - boleh buka trade hidup
  RESEARCH_ONLY - kod/kajian disimpan sebagai penanda aras; TIDAK boleh trade
  DISABLED      - belum dilaksana atau belum diuji; TIDAK boleh trade

Kawalan keselamatan: `guard()` menghentikan bot yang statusnya bukan ACTIVE.
Ia dipanggil pada permulaan bot, jadi strategy yang gagal ujian tidak boleh
hidup secara tidak sengaja.
"""
import json
import os
import time
from pathlib import Path

_DEF = "Z:/opt/trading" if os.name == "nt" else "/opt/trading"
ROOT = Path(os.environ.get("TRADER_ROOT", _DEF))
FILE = ROOT / "state" / "strategy.json"

ACTIVE = "ACTIVE"
RESEARCH_ONLY = "RESEARCH_ONLY"
DISABLED = "DISABLED"
DEMO_ONLY = "DEMO_ONLY"      # boleh dagang pada DEMO sahaja (uji eksekusi)

REGISTRY = {
    "FIBO_V1": dict(
        status=ACTIVE, magic=9150,
        desc="Fade Finobachi: fibo H1 + tapis trend, money-TP, BE",
        note="Strategi aktif. Logik dagangan TIDAK boleh diubah.",
        aliases=("fibo", "fibonacci", "finobachi"),
    ),
    "CHAIN_BREAKOUT_V1": dict(
        status=RESEARCH_ONLY, magic=None,
        desc="Rantai breakout lot-berganda (TP600/SL400, had 3 SL)",
        note="Verdict kajian: RUGI pada semua TF (PF 0.70-0.94), tiada tapis "
             "yang menyelamatkan. Disimpan sebagai penanda aras sahaja.",
        aliases=("chain", "breakout", "chain_v1"),
    ),
    "EMA74_V1": dict(
        status=DISABLED, magic=None,
        desc="EMA74 - belum dilaksana",
        note="Menunggu spec + backtest. Tidak boleh trade.",
        aliases=("ema74", "ema", "ema74_v1"),
    ),
    "STOP_LADDER": dict(
        status=DEMO_ONLY, magic=9200,
        desc="Stop Ladder lot-berganda, cap 0.64 - ujian EKSEKUSI",
        note="Tiada edge terbukti. Demo sahaja: semak slippage stop, kelewatan, "
             "ketepatan paras, pemadaman pending.",
        aliases=("ladder", "stop_ladder", "ladder_v1"),
    ),
}

DEFAULT = "FIBO_V1"
ICON = {ACTIVE: "\U0001f7e2", RESEARCH_ONLY: "\U0001f9ea", DISABLED: "\u26d4"}


def resolve(word: str):
    """Alias/nama -> strategy_id. Pulangkan None kalau tidak dikenali."""
    if not word:
        return None
    w = str(word).strip().upper()
    if w in REGISTRY:
        return w
    for sid, d in REGISTRY.items():
        if w in (a.upper() for a in d.get("aliases", ())) or sid.startswith(w):
            return sid
    return None


def read():
    """Strategy aktif dari cakera. Jatuh balik ke DEFAULT kalau rosak/hilang."""
    try:
        d = json.loads(FILE.read_text(encoding="utf-8"))
        sid = d.get("active")
        if sid in REGISTRY:
            return sid, d
    except Exception:
        pass
    return DEFAULT, {}


def active():
    return read()[0]


def set_active(sid: str):
    if sid not in REGISTRY:
        raise ValueError("strategy tidak dikenali: %s" % sid)
    FILE.parent.mkdir(parents=True, exist_ok=True)
    d = {"active": sid, "updated": int(time.time())}
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2), encoding="utf-8")
    tmp.replace(FILE)                      # atomik: pembaca tak nampak separuh
    return True


def info(sid: str):
    return REGISTRY.get(sid, {})


def tradable(sid: str, is_demo: bool = None) -> bool:
    """ACTIVE sentiasa; DEMO_ONLY hanya bila akaun memang demo."""
    st = info(sid).get("status")
    if st == ACTIVE:
        return True
    if st == DEMO_ONLY:
        return bool(is_demo)
    return False


def guard(sid: str = None, is_demo: bool = None):
    """Hentikan bot jika strategy tidak dibenarkan. Dipanggil pada permulaan bot."""
    sid = sid or os.environ.get("STRATEGY_ID", DEFAULT)
    d = info(sid)
    if not d:
        raise SystemExit("STRATEGY GUARD: id tidak dikenali: %s" % sid)
    if not tradable(sid, is_demo):
        raise SystemExit(
            "STRATEGY GUARD: %s status %s - dilarang buka trade hidup.\n  %s"
            % (sid, d.get("status"), d.get("note", "")))
    return sid


def current_id() -> str:
    """strategy_id untuk dilampirkan pada trade/notifikasi.

    STRATEGY_ID (env) menang - itu yang bot yang berjalan tetapkan. Kalau tidak,
    guna strategy aktif yang disimpan.
    """
    return os.environ.get("STRATEGY_ID") or active()


def line(sid: str):
    d = info(sid)
    cur = active()
    mark = "  <- aktif" if sid == cur else ""
    return "  %s %-18s %-14s %s%s" % (
        ICON.get(d.get("status"), "\u26aa"), sid, d.get("status"), d.get("desc", ""), mark)


def listing():
    out = []
    for sid in REGISTRY:
        out.append(line(sid))
    return out
