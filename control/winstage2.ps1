# CyberFIBO - Windows VPS: sediakan kawalan Telegram (langkah 2)
#
# Stage 1 skrip (winboot.ps1) menghidupkan OpenSSH. Skrip ini pula menyediakan
# SEMUA yang lain di Windows, dalam satu tempat supaya ia boleh diulang dan
# disemak - bukan skrip sementara dalam %TEMP%.
#
#   C:\Python311          python boleh-alih + pip (JANGAN guna pemasang senyap:
#                         ia tersekat tanpa console pada Server 2023 CN)
#   C:\cyberfibo          repo (ZIP dari GitHub - tiada git diperlukan)
#   C:\cyberfibo\.env     TG_TOKEN / TG_CHAT / TG_ALLOWED (mode: hanya admin)
#   SSL_CERT_FILE         sijil certifi - imej ini ada sijil akar disuntik,
#                         jadi TLS gagal tanpa ini
#   Tugasan berjadual      SYSTEM + AtStartup, jadi ia hidup selepas reboot
#                         tanpa sesi desktop:
#                           CyberFIBO-CtlBot, CyberFIBO-CtlNotify
#
# Guna: powershell -File winstage2.ps1 -TgToken <token> -TgChat <chat_id>
param(
    [string]$TgToken = '',
    [string]$TgChat  = '',
    [string]$Root    = 'C:\cyberfibo',
    [switch]$Start
)
$ErrorActionPreference = 'Continue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$py = 'C:\Python311\python.exe'

function Say($m) { Write-Host "== $m" }

# ---------------------------------------------------------------- 1. python
if (-not (Test-Path 'C:\Python311\lib\site-packages')) {
    Say 'sediakan site-packages (perlu sebelum pip berfungsi)'
    New-Item -ItemType Directory -Force -Path 'C:\Python311\Lib\site-packages' | Out-Null
    $pth = 'C:\Python311\python311._pth'
    $t = (Get-Content $pth -Raw) -replace '#\s*import site', 'import site'
    if ($t -notmatch [regex]::Escape('Lib\site-packages')) { $t += "Lib\site-packages`r`n" }
    Set-Content -Path $pth -Value $t -Encoding ASCII
}
if (-not (Test-Path $py)) { Say 'GAGAL: Python tiada'; exit 1 }
Say "python: $(& $py --version 2>&1)"
try { & $py -m pip --version | Out-Null } catch {
    Say 'pasang pip'
    $gp = Join-Path $env:TEMP 'get-pip.py'
    Invoke-WebRequest 'https://bootstrap.pypa.io/get-pip.py' -OutFile $gp -UseBasicParsing
    & $py $gp --no-warn-script-location 2>&1 | Select-Object -Last 2
}

# ---------------------------------------------------------------- 2. paket
Say 'pasang paket python'
& $py -m pip install --disable-pip-version-check --quiet --no-warn-script-location `
    MetaTrader5 numpy pandas certifi 2>&1 | Select-Object -Last 2

# ---------------------------------------------------------------- 3. sijil
# Imej ini membawa sijil akar disuntik -> urllib menolak Telegram TLS.
# Perbaiki dengan CA awam, JANGAN matikan pengesahan.
$ca = & $py -c "import certifi; print(certifi.where())"
Say "CA: $ca"
[Environment]::SetEnvironmentVariable('SSL_CERT_FILE', $ca, 'Machine')
$env:SSL_CERT_FILE = $ca

# ---------------------------------------------------------------- 4. repo
if (-not (Test-Path "$Root\bot\fiboscalper.py")) {
    Say 'ambil repo (ZIP)'
    $z = Join-Path $env:TEMP 'cyberfibo.zip'
    Invoke-WebRequest 'https://codeload.github.com/zarinjas/CyberFIBO/zip/refs/heads/main' -OutFile $z -UseBasicParsing
    $x = Join-Path $env:TEMP 'cf-x'
    if (Test-Path $x) { Remove-Item $x -Recurse -Force }
    Expand-Archive $z -DestinationPath $x -Force
    $src = (Get-ChildItem $x -Directory | Select-Object -First 1).FullName
    New-Item -ItemType Directory -Force -Path $Root | Out-Null
    Copy-Item "$src\*" $Root -Recurse -Force
}
foreach ($d in 'state', 'logs', 'data', 'arms', 'bin') {
    New-Item -ItemType Directory -Force -Path "$Root\$d" | Out-Null
}
Say "repo: $Root"

# ---------------------------------------------------------------- 5. .env
if ($TgToken) {
    $txt = "TG_TOKEN=$TgToken`r`nTG_CHAT=$TgChat`r`nTG_ALLOWED=$TgChat`r`nTRADER_ROOT=C:/cyberfibo`r`n"
    Set-Content -Path "$Root\.env" -Value $txt -Encoding ASCII
    icacls "$Root\.env" /inheritance:r /grant 'Administrators:F' /grant 'SYSTEM:F' | Out-Null
    Say ('.env ditulis: ' + (Get-Item "$Root\.env").Length + ' bait (nilai tidak dipaparkan)')
}

# ---------------------------------------------------------------- 6. wrapper
$caEsc = $ca
$bot = @"
`$py = '$py'
`$env:TRADER_ROOT = 'C:/cyberfibo'
`$env:SSL_CERT_FILE = '$caEsc'
Set-Location '$Root'
while (`$true) {
    & `$py '$Root\control\traderctl' bot 2>&1 | Add-Content '$Root\logs\ctl-bot.out'
    Start-Sleep -Seconds 10
}
"@
$ntf = @"
`$py = '$py'
`$env:TRADER_ROOT = 'C:/cyberfibo'
`$env:SSL_CERT_FILE = '$caEsc'
Set-Location '$Root'
while (`$true) {
    & `$py '$Root\control\traderctl' notify 2>&1 | Add-Content '$Root\logs\ctl-notify.out'
    Start-Sleep -Seconds 10
}
"@
Set-Content -Path "$Root\bin\run-ctlbot.ps1"    -Value $bot -Encoding ASCII
Set-Content -Path "$Root\bin\run-ctlnotify.ps1" -Value $ntf -Encoding ASCII
Say 'wrapper ditulis (TRADER_ROOT + SSL_CERT_FILE diset)'

# ---------------------------------------------------------------- 7. tugasan
Say 'tugasan berjadual (SYSTEM + AtStartup)'
function Install-Task($name, $script) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    $a = New-ScheduledTaskAction -Execute 'powershell.exe' `
            -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`""
    $t = New-ScheduledTaskTrigger -AtStartup
    $p = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $name -Action $a -Trigger $t -Principal $p -Settings $s | Out-Null
    Say "   $name dipasang"
}
Install-Task 'CyberFIBO-CtlBot'    "$Root\bin\run-ctlbot.ps1"
Install-Task 'CyberFIBO-CtlNotify' "$Root\bin\run-ctlnotify.ps1"

if ($Start) {
    Start-ScheduledTask -TaskName 'CyberFIBO-CtlBot'
    Start-ScheduledTask -TaskName 'CyberFIBO-CtlNotify'
    Say 'tugasan dimulakan'
}

Say 'ringkasan'
Write-Host ("   python       : " + (Test-Path $py))
Write-Host ("   certifi CA   : " + (Test-Path $ca))
Write-Host ("   repo         : " + (Test-Path "$Root\bot\fiboscalper.py"))
Write-Host ("   .env         : " + (Test-Path "$Root\.env"))
$ok = & $py "$Root\control\traderctl" tgcheck 2>&1
$ok | ForEach-Object { Write-Host "   $_" }
Write-Host ''
Write-Host 'STAGE2 SIAP'
