# CyberFIBO

Fibo + Heikin-Ashi scalper untuk XAUUSD, dengan kawalan penuh melalui Telegram.

Trading stack ini **berasingan sepenuhnya** daripada aplikasi lain di VPS yang sama
(mykiz.my / mykiznext / nginx / postgres). Tiada port dikongsi, tiada fail dikongsi,
tiada servis dikongsi — dan setiap unit ada had memori keras supaya beban trading
tidak boleh menjejaskan aplikasi lain.

---

## Isi kandungan

| Folder | Apa |
|---|---|
| `bot/` | Scalper MT5 (`fiboscalper.py`), pustaka (`mt5lib.py`), backtester (`fibo_ha_backtest.py`), jambatan state (`traderbridge.py`), ujian |
| `control/` | Kawalan Telegram — `traderctl` (satu fail, stdlib sahaja), login broker, skrip pemasangan Linux + Windows |
| `studies/` | Kajian penyelidikan (trend matrix, dsb.) |
| `docs/` | Seni bina + penemuan penting |

---

## Strategi (ringkas)

Kaedah fibo + Heikin-Ashi, timeframe M5/M15/H1:

* **Group** dibina daripada candle berturutan. Bullish → `0.0` di wick tertinggi,
  `100.0` di wick terendah → **fade = SELL**. Bearish → terbalik → **fade = BUY**.
* Level: `0.0 · 23.6 · 38.2 (ENTRY) · 50 · 61.8 · 100 · 161.8+` — **tanpa 78.6**.
* Zon masuk: `0 – 50 %` dari `0.0` (38.2 bila ada gap).
* Kitaran **mati** bila candle **TUTUP** melepasi `0.0` (VOID). Kitaran **lengkap**
  pada `161.8`.
* **Satu fibo pada satu masa** pada carta.
* Tapisan yang disahkan menambah nilai: **TREND** (selaras arah), **BREAKOUT** (H1 sahaja).
* `tp_pad = 25pt` — TP berhenti 25pt sebelum level (elak spread/requote).
* Breakeven pada `+500` (pilihan), money-TP (`+500 / +700`) menang berbanding tunggu level.

> ⚠️ **Bias backtest yang wajib dielak** — lihat `docs/FINDINGS.md`. Fill pada harga
> OPEN bar isyarat mustahil didapati oleh bot sebenar (PF 2.29 → 0.99). Backtest
> mesti guna `fill="close"`.

---

## Pemasangan Pantas (Linux + Wine)

```bash
# 1. kod
git clone https://github.com/zarinjas/CyberFIBO.git /opt/trading-src
sudo mkdir -p /opt/trading && sudo chown -R $USER /opt/trading
cp -r /opt/trading-src/bot      /opt/trading/mt5
cp    /opt/trading-src/control/* /opt/trading/bin/

# 2. kredensial kawalan Telegram
cp .env.example /opt/trading/.env      # isi TG_TOKEN + TG_CHAT
chmod 600 /opt/trading/.env

# 3. servis
bash control/install_control.sh        # traderctl + trader-bot + trader-notify

# 4. sahkan
traderctl tgcheck
systemctl --user status trader-bot trader-notify
```

## Pemasangan di Windows

Kod ini **stdlib Python sahaja** — `traderctl`, `traderbridge.py` dan bot scalper
jalan tanpa ubah di Windows. Pada Windows, MT5 Python API disokong **rasmi** —
tiada Wine, tiada helah. Langkah penuh: **[docs/WINDOWS.md](docs/WINDOWS.md)**.

---

## Kawalan Telegram — 12 arahan

Butang inline + arahan teks, dua-dua berfungsi:

| Butang | Teks | Apa |
|---|---|---|
| 🔐 LOGIN BROKER | `/login SERVER LOGIN PASSWORD` | Log masuk akaun broker (mesej dipadam serta-merta) |
| 🔄 SWITCH BROKER | `switch` | Tukar antara broker tersimpan (tanpa taip password semula) |
| 📊 STATUS | `status` | Balance, equity, floating, keadaan setiap arm |
| 🎯 NEXT SETUP | `next` | Apa yang setiap arm tunggu sekarang |
| 📈 OPEN TRADES | `trades` | Posisi terbuka (ticket, side, lot, SL, TP, profit) |
| ❓ WHY NO TRADE | `why` | **Sebab konkrit** tiada trade — bukan "tak ada apa-apa" |
| ⏸ PAUSE | `pause` | Henti buka leg baharu (posisi sedia ada kekal) |
| ▶️ RESUME | `resume` | Benarkan masuk semula |
| ⏭ SKIP GROUP | `skip` | Tandakan group semasa habis, cari yang seterusnya |

Penggera automatik: setiap **entry / exit / void / error** ditolak ke Telegram
oleh `trader-notify` (dengar `events.jsonl`).

---

## Kontrak State

Tiga fail, satu arah — bot menulis, kawalan membaca:

| Fail | Ditulis oleh | Dibaca oleh | Isi |
|---|---|---|---|
| `state/arms.json` | bot (`traderbridge.pub_arm` / `pub_account`) | `traderctl status/next/trades/why` | balance, equity, floating, posisi, keadaan setiap arm |
| `state/control.json` | `traderctl` / butang Telegram | bot (`traderbridge.control`) | `{paused, skip, close_all}` |
| `events.jsonl` | bot (`traderbridge.event`) | `trader-notify` | satu JSON per baris → ditolak ke Telegram |

**Peraturan lulus:** `state/arms.json` ditulis secara **atomik** (fail sementara +
`rename`) supaya `traderctl` tidak pernah baca separuh fail.
Setiap arm menerbitkan **posisinya sendiri sahaja** (padan pada `arm`), jadi
N arm tidak menimpa satu sama lain.

---

## Isolasi daripada aplikasi lain

```
✗ tiada port didedahkan ke luar (kawalan = keluar sahaja ke Telegram)
✗ tiada perubahan firewall
✗ tiada DB dikongsi, tiada web server dikongsi, tiada fail dikongsi
✓ setiap unit: MemoryMax + MemoryHigh + CPUWeight=50 + OOMScoreAdjust=600

cgroup v2 → MemoryMax memberi OOM-kill TERHAD dalam cgroup
            → beban trading boleh membunuh DIRINYA SENDIRI,
              tidak pernah nginx / postgres / aplikasi lain
OOMScoreAdjust=600 → unit trading jadi mangsa pilihan kernel
```

Sahkan bila-bila masa:

```bash
systemctl is-active nginx mykiznext postgresql@18-main
systemctl show mykiznext -p NRestarts --value      # mesti 0
cat /proc/pressure/memory                          # mesti ~0.00
sudo journalctl -k --since '1 hour ago' | grep -ci 'out of memory'   # mesti 0
```

---

## Ujian

```bash
cd bot && pytest -q          # suite kanonik
```

## Lesen

Peribadi — bukan untuk edaran.
