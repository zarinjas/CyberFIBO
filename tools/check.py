#!/usr/bin/env python3
"""Satu titik masuk verifikasi — jalan di Windows, macOS dan Linux tanpa `make`.

    python tools/check.py        (atau: make check / make test)

Tiga langkah: sintaks, suite pytest, dan pemeriksaan paparan akaun.
Keluar dengan kod bukan-sifar kalau ada yang gagal, jadi boleh dipakai dalam CI.
"""
import ast
import glob
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
PY = sys.executable
_failed = []


def step(name, fn):
    print("== %s ==" % name)
    try:
        print("   %s" % fn())
    except Exception as e:
        _failed.append(name)
        detail = getattr(e, "message", None) or str(e)
        print("   GAGAL: %s" % detail.strip().splitlines()[-1][:200])


def syntax():
    files = sorted(
        glob.glob(str(REPO / "bot/*.py"))
        + glob.glob(str(REPO / "control/*.py"))
        + glob.glob(str(REPO / "tools/*.py"))
        + glob.glob(str(REPO / "studies/*.py"))
        + [str(REPO / "control/traderctl")]
    )
    for f in files:
        ast.parse(pathlib.Path(f).read_text(encoding="utf-8"))
    return "%d fail, sintaks OK" % len(files)


def tests():
    r = subprocess.run([PY, "-m", "pytest", "-q"], cwd=str(REPO / "bot"),
                       capture_output=True, text=True)
    last = (r.stdout or r.stderr).strip().splitlines()[-1] if (r.stdout or r.stderr) else "tiada output"
    if r.returncode:
        raise RuntimeError(last)
    return last


def display():
    r = subprocess.run([PY, str(REPO / "tools/check_account_display.py")],
                       capture_output=True, text=True, cwd=str(REPO))
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout or "").strip()[:200])
    return "3 laluan paparan disahkan (DEMO / LIVE / TIRUAN)"


step("sintaks", syntax)
step("ujian (pytest)", tests)
step("paparan akaun", display)

print("\n%s" % ("SEMUA LULUS" if not _failed else "GAGAL: " + ", ".join(_failed)))
sys.exit(1 if _failed else 0)
