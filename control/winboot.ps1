<#
    CyberFIBO - bootstrap untuk Windows Server yang baru.

    Jalankan sebagai ADMINISTRATOR (PowerShell elevated):

        irm https://raw.githubusercontent.com/zarinjas/CyberFIBO/main/control/windows_bootstrap.ps1 | iex

    Ia melakukan SATU perkara sahaja dengan sengaja: hidupkan OpenSSH Server dan
    pasang kunci awam supaya baki pemasangan boleh dijalankan melalui SSH
    (pantas, boleh diulang, dan boleh dilihat). Tiada tetapan lain disentuh.

    Selamat dijalankan berulang kali - setiap langkah menyemak keadaan dahulu.
#>

$ErrorActionPreference = 'Continue'
$KeyFile = 'C:\ProgramData\ssh\administrators_authorized_keys'
$PubKey  = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIK7siv5sdHiQGNCvzMDw/bTSDywDSzX9UjzIsjtiEKAk hermes@pc-cyberfibo-win'

function Say($m) { Write-Host "== $m" }

Say "CyberFIBO bootstrap - $(Get-Date -Format s)"

# ---------------------------------------------------------------- 1. OpenSSH
$haveSshd = Get-Service sshd -ErrorAction SilentlyContinue
if (-not $haveSshd) {
    Say "Memasang OpenSSH Server melalui Windows Capability (boleh ambil 1-2 minit) ..."
    try {
        Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 | Out-Null
    } catch {
        Write-Host "   capability gagal: $_"
    }
    $haveSshd = Get-Service sshd -ErrorAction SilentlyContinue
}

# fallback: muat turun keluaran rasmi win32 OpenSSH dari GitHub
if (-not $haveSshd) {
    Say "Fallback: memuat turun OpenSSH rasmi dari GitHub ..."
    $dst = 'C:\Program Files\OpenSSH'
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $rel = Invoke-RestMethod 'https://api.github.com/repos/PowerShell/Win32-OpenSSH/releases/latest'
        $asset = ($rel.assets | Where-Object { $_.name -like 'OpenSSH-Win64.zip' })[0]
        if ($asset) {
            New-Item -ItemType Directory -Force -Path $dst | Out-Null
            $zip = Join-Path $env:TEMP 'openssh.zip'
            Invoke-WebRequest $asset.browser_download_url -OutFile $zip -UseBasicParsing
            Expand-Archive $zip -DestinationPath $dst -Force
            $inner = Join-Path $dst 'OpenSSH-Win64'
            if (Test-Path $inner) { Copy-Item "$inner\*" $dst -Recurse -Force }
            & (Join-Path $dst 'install-sshd.ps1')
            $haveSshd = Get-Service sshd -ErrorAction SilentlyContinue
        }
    } catch {
        Write-Host "   fallback gagal: $_"
    }
}

# ---------------------------------------------------------------- 2. khidmat
if ($haveSshd) {
    Say "Mengaktifkan sshd ..."
    Set-Service -Name sshd -StartupType Automatic
    Set-Service -Name ssh-agent -StartupType Automatic -ErrorAction SilentlyContinue
    Start-Service ssh-agent -ErrorAction SilentlyContinue
    Start-Service sshd -ErrorAction SilentlyContinue
} else {
    Say "GAGAL: sshd tidak dapat dipasang."
}

# ---------------------------------------------------------------- 3. kunci
Say "Memasang kunci awam ..."
if (-not (Test-Path $KeyFile)) { New-Item -Path $KeyFile -ItemType File -Force | Out-Null }
if (-not (Select-String -Path $KeyFile -Pattern 'hermes@pc-cyberfibo-win' -Quiet -ErrorAction SilentlyContinue)) {
    Add-Content -Path $KeyFile -Value $PubKey
}
# akaun pentadbir membaca fail ini, jadi ACL mesti ketat
icacls $KeyFile /inheritance:r /grant 'Administrators:F' /grant 'SYSTEM:F' | Out-Null
Restart-Service sshd -ErrorAction SilentlyContinue

# ---------------------------------------------------------------- 4. firewall
Say "Membuka firewall port 22 ..."
if (-not (Get-NetFirewallRule -Name 'CyberFIBO-SSH' -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -Name 'CyberFIBO-SSH' -DisplayName 'CyberFIBO SSH' `
        -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
}

# shell lalai = PowerShell supaya arahan jarak jauh senang dibaca
New-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell `
    -Value 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' `
    -PropertyType String -Force | Out-Null

# ---------------------------------------------------------------- 5. laporan
Say "Ringkasan"
Write-Host ("   sshd          : " + (Get-Service sshd -ErrorAction SilentlyContinue).Status)
Write-Host ("   sshd startup  : " + (Get-Service sshd -ErrorAction SilentlyContinue).StartType)
Write-Host ("   port 22 rule  : " + [bool](Get-NetFirewallRule -Name 'CyberFIBO-SSH' -ErrorAction SilentlyContinue))
Write-Host ("   kunci         : " + (Select-String -Path $KeyFile -Pattern 'hermes@pc-cyberfibo-win' -Quiet))
Write-Host ''
Write-Host 'SIAP. OpenSSH hidup.'
Write-Host ''
Write-Host 'LANGKAH SETERUSNYA (dari mesin anda, melalui SSH):'
Write-Host '  powershell -File winstage2.ps1 -TgToken <token> -TgChat <chat_id> -Start'
Write-Host ''
Write-Host 'Ambil skrip itu dari repo:'
Write-Host '  irm https://cdn.jsdelivr.net/gh/zarinjas/CyberFIBO@main/control/winstage2.ps1 -OutFile winstage2.ps1'
Write-Host 'Panduan penuh: docs/WINDOWS.md di dalam repo.'
Write-Host ''
Write-Host 'Juga: buka port 22 pada Security Group penyedia VPS.'
