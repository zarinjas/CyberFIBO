"""Semak paparan akaun: laluan SEBENAR dan laluan TIRUAN."""
import json
import os
import pathlib
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
CTL = REPO / "control" / "traderctl"


def run(root, verb):
    env = dict(os.environ, TRADER_ROOT=str(root))
    r = subprocess.run([sys.executable, str(CTL), verb],
                       capture_output=True, text=True, env=env)
    return (r.stdout or r.stderr).rstrip()


# --- akaun sebenar (state palsu, bukan tiruan) -----------------------------
tmp = pathlib.Path(tempfile.mkdtemp())
(tmp / "state").mkdir()
(tmp / "state" / "arms.json").write_text(json.dumps({
    "synthetic": False, "balance": 11799.62, "equity": 12101.10, "floating": 0.0,
    "account": {"login": 5008941, "server": "TradingProInternational-Demo",
                "name": "Hafiz Demo", "currency": "USC", "leverage": 500, "demo": True},
    "positions": [], "control": {"paused": False},
    "arms": [{"name": "H", "magic": 9117, "tf": "H1", "state": "…",
              "action": "menunggu setup", "reason": "harga LUAR zon - perlu turun 12pt"}]},
    indent=2))
print("=== LALUAN AKAUN SEBENAR ===")
print(run(tmp, "status"))

# --- akaun LIVE (bukan demo) ---------------------------------------------
d = json.loads((tmp / "state" / "arms.json").read_text())
d["account"].update(demo=False, login=7771234, server="Broker-Live", name="Hafiz Live")
(tmp / "state" / "arms.json").write_text(json.dumps(d))
print("\n=== LALUAN AKAUN LIVE ===")
print("\n".join(run(tmp, "status").splitlines()[:8]))

# --- data tiruan ----------------------------------------------------------
os.environ["TRADER_ROOT"] = str(tmp)
subprocess.run([sys.executable, str(CTL), "sim"], capture_output=True, text=True,
               env=dict(os.environ, TRADER_ROOT=str(tmp)))
print("\n=== LALUAN TIRUAN ===")
print("\n".join(run(tmp, "status").splitlines()[:9]))
