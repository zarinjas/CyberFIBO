# Pemasangan Windows VPS

MT5 Python API **memerlukan Windows** — ia disokong rasmi di sana. (Di Linux
melalui Wine ia tidak berjaya: `initialize()` gagal dengan `-10005 IPC timeout`
atau `-10003 Pipe server didn't answer`. Lihat `docs/FINDINGS.md`.)

Kedua-dua skrip di bawah **selamat dijalankan berulang kali**.

---

## Langkah 0 — Server baru sahaja

Windows Server tiada SSH secara lalai. Jalankan **sekali** sebagai Administrator
(PowerShell elevated):

```powershell
irm https://cdn.jsdelivr.net/gh/zarinjas/CyberFIBO@main/control/winboot.ps1 | iex
```

Ia menghidupkan OpenSSH Server, memasang kunci awam, dan membuka port 22 pada
firewall Windows. Kemudian buka port 22 pada Security Group penyedia VPS juga.

> RDP: **jangan sign out** — sign out membunuh sesi dan menghentikan MT5.
> Disconnect atau tutup tetingkap adalah selamat.

---

## Langkah 1 — Semua yang lain (melalui SSH)

```powershell
powershell -File winstage2.ps1 -TgToken "<token>" -TgChat "<chat_id>" -Start
```

Apa yang dilakukannya:

| Bahagian | Butiran |
|---|---|
| Python | `C:\Python311` boleh-alih + pip. **Jangan** guna pemasang senyap — ia tersekat tanpa console pada Server 2023 CN |
| Paket | `MetaTrader5 numpy pandas certifi` |
| Sijil | `SSL_CERT_FILE` → CA `certifi`. **Perlu** pada imej ini: terdapat sijil akar disuntik, jadi TLS gagal tanpa ini (`CERTIFICATE_VERIFY_FAILED: self-signed certificate in certificate chain`) |
| Repo | `C:\cyberfibo` dari ZIP GitHub (tiada git diperlukan) |
| Kredensial | `C:\cyberfibo\.env`, ACL terhad kepada Administrators + SYSTEM |
| Auto-start | Tugasan berjadual **SYSTEM + AtStartup**: `CyberFIBO-CtlBot`, `CyberFIBO-CtlNotify` — hidup selepas reboot **tanpa** sesi desktop |

Sahkan:

```powershell
$env:TRADER_ROOT='C:/cyberfibo'
& C:\Python311\python.exe C:\cyberfibo\control\traderctl tgcheck
```

---

## Langkah 2 — MT5 (sekali, di GUI)

1. Buka MT5, **File → Login to Trade Account** dan log masuk.
   Ini **wajib**: pelayan paip API MT5 **tidak bermula** sehingga terminal
   didaftarkan pada akaun. Tanpa log masuk, `initialize()` akan gagal.
2. Tekan **Algo Trading** pada toolbar (atau `Ctrl+E`) sehingga hijau.
   Tanpa ini `trade_allowed` ialah `False` dan **setiap order akan ditolak**.

Sahkan kedua-duanya:

```powershell
& C:\Python311\python.exe -c "import MetaTrader5 as mt5; mt5.initialize(); print(mt5.terminal_info().trade_allowed, mt5.account_info())"
```

---

## Simbol

Simbol yang boleh didagakan pada akaun ini ialah **`XAUUSDc`** (huruf kecil `c`).
`XAUUSD` wujud tetapi `trade_mode=3` (disabled) dengan bid/ask `0.00`.
`mt5lib.SYMBOL` sudah menetapkan nilai yang betul.

---

## Perangkap PowerShell (semuanya pernah memakan kita)

| Perangkap | Gejala | Elakan |
|---|---|---|
| Fail `.ps1` bukan-ASCII | PowerShell 5.1 membaca UTF-8-tanpa-BOM sebagai ANSI; satu em-dash menjadi bait sampah yang menelan quote dan ralat muncul 90 baris kemudian | Kekalkan skrip **ASCII tulen** — `tools/check.py` menguatkuasakannya |
| Garis bawah dalam nama fail yang perlu ditaip | Papan kekunci Cina menukar `_` menjadi ruang → `ParameterBindingException` | Guna nama tanpa garis bawah (`winboot.ps1`, bukan `windows_bootstrap.ps1`) |
| `Start-Transcript` melalui SSH | Ia memerlukan console, jadi ia gagal **senyap** dan tiada log ditulis | Tulis ke fail secara eksplisit, atau biarkan output ke SSH |
| Pemasang Python `/quiet` | Tersekat tanpa console | Guna Python boleh-alih + `get-pip.py` |
| `Stop-Process powershell` | Membunuh tugasan berjadual sendiri juga (`LastTaskResult 267014` = terminated by user) | Tapis mengikut nama atau id |
| MT5 benarkan **satu** klien API | Ujian serentak memberi `-10002 IPC recv failed` | Bunuh ujian lama sebelum menjalankan yang baharu |
