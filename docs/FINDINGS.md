# Penemuan penting

Rekod keputusan yang mengubah arah projek, dengan bukti. Ditulis supaya kita
tidak mengulangi kerja yang sama atau mempercayai nombor yang salah.

---

## 1. 🔴 Bias fill — punca semua keputusan struktur lama dibatalkan

**Masalah:** backtester asal masuk pada harga **OPEN** bar isyarat
(`fibo_ha_backtest.py` baris 166: `entry = o[i_entry]`). Harga itu **mustahil
didapati** oleh bot sebenar — bot bertindak ~30 saat selepas candle ditutup.

**Bukti:** satu setup yang sama, dua andaian fill:

| Fill | Setup | Win | PF |
|---|---|---|---|
| `open` (salah) | 1,408 | 93.2 % | **2.29** |
| `close` (betul) | 215 | 61.9 % | **0.99** |

**Kesan:** semua keputusan lama (`hi_len=40`, zon 38.2 → 50, exit `100.0`
terbaik, "5 lapis menang") **dibatalkan** — ia berdiri atas nombor palsu.

**Peraturan sekarang:** backtest **mesti** guna `fill="close"`.
Parameter itu ada dalam `fibo_ha_backtest.run(fill=...)` dengan amaran dalam docstring.

---

## 2. 🔴 Lookahead pada indeks trend

**Masalah:** `searchsorted(h1_times, t5[i], 'right') - 1` memilih bar H1 yang
**sedang terbentuk** — 0 hingga 59 minit maklumat masa depan.

**Bukti:** PF jatuh 3.16 → 1.44 (H1) dan 1.69 → 1.32 (M15) sebaik sahaja
indeks dibetulkan kepada bar yang **sudah ditutup**.

**Peraturan sekarang:** bar trend mesti **TUTUP** sebelum saat masuk.
Ada dalam `trend_matrix_study.py`.

---

## 3. 🔴 MT5 + Wine = GAGAL (IPC timeout) — ini yang menentukan seni bina

**Soalan:** bolehkah MT5 Python API jalan atas Linux melalui Wine?

**Jawapan: TIDAK.**

Yang **berjaya**:
```
✓ Wine 10.0 dipasang pada Ubuntu 26.04
✓ MT5 5.0 dipasang dalam prefix
✓ terminal64.exe BERJALAN di bawah Xvfb
✓ Python 3.11.9 (Windows, embeddable) jalan dalam prefix
✓ MetaTrader5 5.0.6180 + numpy + pip terpasang
✓ API berjaya MELANCARKAN terminal64.exe sendiri
```

Yang **gagal**:
```
✗ mt5.initialize()  →  (-10005, 'IPC timeout')   91.6s
✗ cuba semula (wineserver bersih) → (-10005, 'IPC timeout')   90.5s
```

**Analisis:** bukan isu CRT — modul memuat ✓ dan memulangkan **kod ralat MT5
yang sah** ✓. Yang gagal ialah **lapisan IPC** (handshake tetingkap/paip antara
API dan terminal) yang Wine tidak tiru.

**Kesimpulan:** MT5 memerlukan **hos Windows sebenar**. Di Windows, API ini
disokong rasmi dan tiada helah diperlukan.

### Sub-penemuan: numpy 2.x mematikan Wine

```
wine: Call to unimplemented function ucrtbase.dll.crealf, aborting
```
`crealf` ialah fungsi C99 complex-math yang numpy 2.x panggil dan Wine 10 belum
laksanakan. **Penyelesaian: pin `numpy<2`** (1.26.4 disahkan berfungsi).
Ia juga meninggalkan `winedbg` tersangkut yang mencemar wineserver —
bunuh dengan `wineserver -k` sebelum cubaan seterusnya.

---

## 4. Tapisan yang disahkan menambah nilai

| Tapisan | Kesan | Nota |
|---|---|---|
| **TREND** (selaras arah) | PF 0.99 → 1.30 (M5) · 2.03 → **4.80** (H1) | Peningkatan terbesar. 58 % setup asal **lawan** trend |
| **BREAKOUT** | PF 2.03 → 2.57 (H1) | **RUGI** pada M15 (3.83 → 3.29) — guna H1 sahaja |
| S/R | tiada kesan | Dibuang |
| STRUCTURE | kecil | Tidak diguna |
| **5 lapis** | PF **2.55** vs 1 lapis + basket 500 → **3.23** | Lapisan **KALAH** |

---

## 5. Exit: money-TP menang

Menunggu level fibo memberi +66 USC/hari. Menutup ikut **untung** memberi lebih:

| Sasaran | USC/hari |
|---|---|
| +500 | 193 |
| +700 | 246 |

> Nombor asal (1,000–1,500) terlalu rendah — basket sepatutnya **2,500–3,500**
> (≈500+ setiap posisi).

---

## 6. Bug yang pernah menelan wang (jangan ulang)

| Bug | Gejala | Pembetulan |
|---|---|---|
| Gate lapisan | 3 lapis masuk 3 leg pada harga hampir sama → 15 % risiko, bukan 5 % | `s.lvl >= step*(n+1)` + risiko dibahagi bilangan lapisan |
| Cawangan SL pulang PnL positif | Simulator lapor win 95–98 %, PF 23–66 | Tanda dibetulkan + invariant |
| Laluan TP mati | `first = tie` ("sl") menghalang cawangan TP | Susunan keputusan dibetulkan |
| Path `control.json` relatif pada dir bot | Butang PAUSE/SKIP tulis ke fail yang bot tidak baca | `traderbridge.CTRL` → `/opt/trading/state/control.json` |

---

## 7. Persekitaran: dua VPS yang mudah dikelirukan

| | 43.159.60.219 | **43.156.60.15** |
|---|---|---|
| Peranan | mykiz.my / CyberPanel | **trading + KIZ kedua** |
| OS | Ubuntu 24.04 | Ubuntu 26.04 |
| RAM guna | swap 1.1 GB terpakai ⚠️ | lega ✓ |
| **OOM (30 hari)** | **3 kali** ✗ (mangsa: proses KIZ) | **0** ✓ |
| Hostname | `VM-0-10-ubuntu` | `VM-0-10-ubuntu` ← **sama!** |

> ⚠️ **Hostname dua-dua sama** — jangan sesekali pilih hos ikut hostname.
> Sahkan dengan IP + kehadiran fail penanda.

**Jangan sekali-kali deploy trading pada 43.159.60.219** — kotak itu sudah
kehabisan memori dan membunuh aplikasi KIZ tiga kali, tanpa sebarang beban trading.
