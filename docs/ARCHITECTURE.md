# Seni bina

## Dua zon, satu dinding keras

```
VPS 43.156.60.15 — ZON APLIKASI (TIDAK DISENTUH)
├─ nginx :80 :443
├─ mykiznext.service      Next.js, user mykiz7808, 127.0.0.1:3010
├─ postgresql@18          127.0.0.1:5432
└─ (user units lain)

ZON TRADING (berasingan sepenuhnya)
├─ /opt/trading/
│   ├─ bin/     traderctl · mt5_login.sh|.py · run_arm.sh · install_*.sh
│   ├─ mt5/     fiboscalper.py · mt5lib.py · traderbridge.py · tests/
│   ├─ state/   arms.json · control.json · brokers.json
│   ├─ logs/
│   ├─ data/    backup, kajian
│   ├─ arms/    H.env · L.env  (magic + args setiap arm)
│   ├─ events.jsonl
│   └─ .env     (600)
└─ systemd user units:
    trader-bot.service        butang Telegram          MemoryMax 150M
    trader-notify.service     events.jsonl → Telegram  MemoryMax 100M
    xvfb99.service            display maya :99         MemoryMax  80M
    mt5-term.service          MT5 dalam Wine           MemoryMax 900M
    fiboscalper@.service      satu per arm             MemoryMax 250M
```

## Arah data

```
                    ┌─────────────────────────────┐
   Telegram  ◄──────┤ trader-bot   (butang/arahan)│
   (keluar)         │ trader-notify(notifikasi)   │
                    └──────────┬──────────────────┘
                               │ baca/tulis
                        ┌──────▼──────┐
                        │   state/    │  arms.json · control.json
                        └──────┬──────┘
                               │
                    ┌──────────▼──────────┐
                    │ traderbridge.py     │  (dalam bot)
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │ fiboscalper.py      │  strategi kau
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │ MT5 (terminal64)    │
                    └─────────────────────┘
```

Arah penting:

* **Kawalan → bot**: `control.json`. Bot membacanya **setiap poll** (30s) —
  jadi `pause` / `skip` / `close_all` berkuat kuasa dalam ~30 saat.
* **Bot → kawalan**: `arms.json` (atomik) + `events.jsonl` (append).
* **Notifikasi tidak bergantung pada bot kawalan**: `trader-notify` hanya
  membaca fail. Kalau ia mati, bot terus berjalan. Kalau bot mati,
  notifikasi pendam tetapi tidak merosakkan apa-apa.

## Rangka jaminan memori

Nginx / postgres / aplikasi berjalan dalam cgroup mereka sendiri. Setiap unit
trading ada `MemoryMax` **dan** `OOMScoreAdjust=600`:

```
cgroup v2  →  MemoryMax melaksanakan OOM-kill TERHAD dalam cgroup itu
              → beban trading membunuh DIRINYA SENDIRI sahaja
OOMScoreAdjust=600  →  kernel pilih unit trading dulu, bukan aplikasi lain
```

Hasilnya: walaupun MT5 membengkak, aplikasi lain **tidak boleh** menjadi mangsa.
Ini bukan janji — ia sifat cgroup v2.

## Mengapa Telegram dan bukan LLM

`traderctl` **sengaja** tiada LLM:

| Sebab | Kesan |
|---|---|
| Tiada kos token | Butang percuma, beribu kali sehari pun |
| Tiada kunci API perlu disimpan di VPS | Permukaan serangan lebih kecil |
| Jawapan serta-merta | Tiada giliran model |
| Hidup walaupun Hermes mati | Kawalan tidak bergantung pada ejen |

Ejen (LLM) berguna untuk **analisis** ("kenapa PF turun?"), bukan untuk butang.

## Berkongsi antara dua arm

Setiap arm ada `FIBOSCALPER_STATE=state/ab_<ARM>.json` sendiri (jejak kitaran
sendiri) tetapi **berkongsi** `state/arms.json`. `traderbridge.pub_account`
memadankan pada `arm` supaya setiap arm menerbitkan posisinya sendiri sahaja —
tanpa ini, arm terakhir yang menulis akan memadam baris arm lain.
