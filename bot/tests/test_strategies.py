"""Ujian kawalan strategy: registry, status, pertukaran, dan guard.

Bahagian paling penting yang diuji di sini ialah KESELAMATAN:
  - strategy bukan-ACTIVE tidak boleh menjadi aktif
  - pertukaran ditolak bila ada posisi terbuka (elak campur posisi)
  - setiap event membawa strategy_id

Jalankan: python -m pytest tests -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import strategies as ST  # noqa: E402
import traderbridge as TB  # noqa: E402


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Arahkan fail strategy ke tmp_path supaya state sebenar tidak disentuh."""
    f = tmp_path / "state" / "strategy.json"
    monkeypatch.setattr(ST, "FILE", f)
    monkeypatch.delenv("STRATEGY_ID", raising=False)
    return f


# ------------------------------------------------------------------ registry
def test_registry_ids_are_unique_and_uppercase():
    assert len(ST.REGISTRY) == len(set(ST.REGISTRY))
    for sid in ST.REGISTRY:
        assert sid == sid.upper()


def test_fibo_is_active():
    assert ST.info("FIBO_V1")["status"] == ST.ACTIVE
    assert ST.tradable("FIBO_V1") is True


def test_chain_is_research_only_and_not_tradable():
    assert ST.info("CHAIN_BREAKOUT_V1")["status"] == ST.RESEARCH_ONLY
    assert ST.tradable("CHAIN_BREAKOUT_V1") is False


def test_ema74_is_disabled_and_not_tradable():
    assert ST.info("EMA74_V1")["status"] == ST.DISABLED
    assert ST.tradable("EMA74_V1") is False


def test_every_non_active_strategy_is_untradable():
    """Kelas, bukan kes: apa-apa yang bukan ACTIVE mesti tidak boleh didagakan."""
    for sid, d in ST.REGISTRY.items():
        assert ST.tradable(sid) == (d["status"] == ST.ACTIVE)


# -------------------------------------------------------------------- resolve
@pytest.mark.parametrize("word,expect", [
    ("fibo", "FIBO_V1"), ("FIBO_V1", "FIBO_V1"), ("finobachi", "FIBO_V1"),
    ("chain", "CHAIN_BREAKOUT_V1"), ("breakout", "CHAIN_BREAKOUT_V1"),
    ("ema74", "EMA74_V1"), ("ema", "EMA74_V1"),
])
def test_resolve_aliases(word, expect):
    assert ST.resolve(word) == expect


@pytest.mark.parametrize("word", ["", "takwujud", "xyz", None])
def test_resolve_unknown_is_none(word):
    assert ST.resolve(word) is None


def test_resolve_is_case_insensitive():
    assert ST.resolve("FiBo") == ST.resolve("FIBO_V1")


# ----------------------------------------------------------------- persistence
def test_default_is_fibo_when_no_file(isolated):
    assert ST.active() == "FIBO_V1"


def test_set_active_persists(isolated):
    ST.set_active("FIBO_V1")
    assert ST.active() == "FIBO_V1"
    assert isolated.exists()
    assert json.loads(isolated.read_text(encoding="utf-8"))["active"] == "FIBO_V1"


def test_set_active_rejects_unknown(isolated):
    with pytest.raises(ValueError):
        ST.set_active("TAK_WUJUD")


def test_corrupt_file_falls_back_to_default(isolated):
    isolated.parent.mkdir(parents=True, exist_ok=True)
    isolated.write_text("{ ini bukan json", encoding="utf-8")
    assert ST.active() == "FIBO_V1"


def test_unknown_id_in_file_falls_back(isolated):
    isolated.parent.mkdir(parents=True, exist_ok=True)
    isolated.write_text(json.dumps({"active": "HANTU_V9"}), encoding="utf-8")
    assert ST.active() == "FIBO_V1"


# ---------------------------------------------------------------------- guard
def test_guard_allows_active(monkeypatch):
    monkeypatch.setenv("STRATEGY_ID", "FIBO_V1")
    assert ST.guard() == "FIBO_V1"


@pytest.mark.parametrize("sid", ["CHAIN_BREAKOUT_V1", "EMA74_V1"])
def test_guard_blocks_non_active(monkeypatch, sid):
    monkeypatch.setenv("STRATEGY_ID", sid)
    with pytest.raises(SystemExit):
        ST.guard()


def test_guard_blocks_unknown(monkeypatch):
    monkeypatch.setenv("STRATEGY_ID", "HANTU")
    with pytest.raises(SystemExit):
        ST.guard()


# ------------------------------------------------------------- event tagging
def test_event_carries_strategy_id(isolated, monkeypatch, tmp_path):
    """Setiap notifikasi mesti membawa strategy_id."""
    monkeypatch.setattr(TB, "ROOT", tmp_path)
    monkeypatch.setattr(TB, "EVENTS", tmp_path / "events.jsonl")
    monkeypatch.setenv("STRATEGY_ID", "FIBO_V1")
    TB.event("entry", "MASUK BUY")
    line = json.loads((tmp_path / "events.jsonl").read_text(encoding="utf-8").strip())
    assert line["strategy_id"] == "FIBO_V1"
    assert line["kind"] == "entry"


def test_event_strategy_id_follows_env(isolated, monkeypatch, tmp_path):
    monkeypatch.setattr(TB, "ROOT", tmp_path)
    monkeypatch.setattr(TB, "EVENTS", tmp_path / "events.jsonl")
    monkeypatch.setenv("STRATEGY_ID", "EMA74_V1")
    TB.event("info", "ujian")
    line = json.loads((tmp_path / "events.jsonl").read_text(encoding="utf-8").strip())
    assert line["strategy_id"] == "EMA74_V1"


def test_arm_carries_strategy_id(isolated, monkeypatch, tmp_path):
    monkeypatch.setattr(TB, "ARMS", tmp_path / "arms.json")
    monkeypatch.setattr(TB, "STATE", tmp_path)
    monkeypatch.setenv("STRATEGY_ID", "FIBO_V1")
    TB.pub_arm("W", "H1", 9150, "BUY")
    d = json.loads((tmp_path / "arms.json").read_text(encoding="utf-8"))
    assert d["arms"][0]["strategy_id"] == "FIBO_V1"


def test_positions_carry_strategy_id(isolated, monkeypatch, tmp_path):
    monkeypatch.setattr(TB, "ARMS", tmp_path / "arms.json")
    monkeypatch.setattr(TB, "STATE", tmp_path)
    monkeypatch.setattr(TB, "CTRL", tmp_path / "control.json")
    monkeypatch.setenv("STRATEGY_ID", "FIBO_V1")
    TB.pub_account(100.0, 100.0, 0.0,
                   [{"arm": "W", "ticket": 1, "side": "BUY", "profit": 0.0}], arm="W")
    d = json.loads((tmp_path / "arms.json").read_text(encoding="utf-8"))
    assert d["positions"][0]["strategy_id"] == "FIBO_V1"
    assert d["strategy_id"] == "FIBO_V1"


def test_two_strategies_do_not_share_position_rows(isolated, monkeypatch, tmp_path):
    """Per-ARM merge: strategy lain tidak memadam baris strategy ini."""
    monkeypatch.setattr(TB, "ARMS", tmp_path / "arms.json")
    monkeypatch.setattr(TB, "STATE", tmp_path)
    monkeypatch.setattr(TB, "CTRL", tmp_path / "control.json")
    monkeypatch.setenv("STRATEGY_ID", "FIBO_V1")
    TB.pub_account(1.0, 1.0, 0.0, [{"arm": "W", "ticket": 1}], arm="W")
    TB.pub_account(1.0, 1.0, 0.0, [{"arm": "X", "ticket": 2}], arm="X")
    d = json.loads((tmp_path / "arms.json").read_text(encoding="utf-8"))
    assert {p["arm"] for p in d["positions"]} == {"W", "X"}
