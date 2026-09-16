#!/bin/bash
# NOTA: skrip ini khusus Linux+Wine (menjana unit systemd). Pada Windows, MT5
# berjalan asli — guna Task Scheduler/NSSM dan langkau skrip ini sepenuhnya.
# Install the MT5 + scalper units. Memory-capped and OOM-scored so a memory
# squeeze can only ever kill OUR processes, never mykiznext.
set -e

D=/opt/trading
U="$HOME/.config/systemd/user"
mkdir -p "$U" "$D/arms" "$D/logs"

# Xvfb helper (MT5 needs a display; :99 is created by this script's unit)
cat > "$D/bin/xvfb-up.sh" <<'EOF'
#!/bin/bash
pgrep -f "Xvfb :99" >/dev/null && exit 0
exec Xvfb :99 -screen 0 1280x800x24 -nolisten tcp
EOF
chmod 755 "$D/bin/xvfb-up.sh"

cat > "$U/xvfb99.service" <<'EOF'
[Unit]
Description=Virtual display :99 for MetaTrader 5
After=network-online.target

[Service]
Type=simple
ExecStart=/opt/trading/bin/xvfb-up.sh
Restart=always
RestartSec=5
MemoryMax=80M
CPUWeight=20

[Install]
WantedBy=default.target
EOF

cat > "$U/mt5-term.service" <<'EOF'
[Unit]
Description=MetaTrader 5 terminal (Wine)
After=xvfb99.service network-online.target
Requires=xvfb99.service

[Service]
Type=simple
Environment=WINEPREFIX=/opt/trading/wineprefix
Environment=WINEDEBUG=-all
Environment=DISPLAY=:99
Environment=WINEDLLOVERRIDES=mscoree,mshtml=
ExecStart=/usr/bin/wine "C:/Program Files/MetaTrader 5/terminal64.exe" /portable
Restart=always
RestartSec=15
# hard ceilings: this cgroup can be OOM-killed on its own without touching
# anything outside it (cgroup v2 -> cgroup-local kill)
MemoryMax=900M
MemoryHigh=700M
CPUWeight=50
OOMScoreAdjust=600

[Install]
WantedBy=default.target
EOF

cat > "$D/bin/run_arm.sh" <<'EOF'
#!/bin/bash
# Run one scalper arm with the Windows Python inside the Wine prefix - the
# MetaTrader5 module only exists for Windows, so Python must live in the prefix.
set -e
A="$1"
[ -f "/opt/trading/arms/$A.env" ] || { echo "no config for arm $A"; exit 1; }
set -a; . "/opt/trading/arms/$A.env"; set +a
export WINEPREFIX=/opt/trading/wineprefix WINEDEBUG=-all DISPLAY=:99
export FIBOSCALPER_TAG="$A"
export FIBOSCALPER_STATE="/opt/trading/state/ab_$A.json"
export TRADER_ROOT="Z:/opt/trading"
cd /opt/trading/mt5
exec wine "C:/Python311/python.exe" -u fiboscalper.py --magic "$MAGIC" $ARGS
EOF
chmod 755 "$D/bin/run_arm.sh"

cat > "$U/fiboscalper@.service" <<'EOF'
[Unit]
Description=CyberFibo scalper arm %i
After=mt5-term.service
Requires=mt5-term.service

[Service]
Type=simple
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/trading/bin/run_arm.sh %i
Restart=always
RestartSec=15
MemoryMax=250M
MemoryHigh=180M
CPUWeight=50
OOMScoreAdjust=600

[Install]
WantedBy=default.target
EOF

# starter arm configs (mirror what is running on the PC today)
cat > "$D/arms/H.env" <<'EOF'
MAGIC=9117
ARGS=--fibo-tf H1 --trend --money-tp 700 --be-at 500 --risk-pct 2.0
EOF
cat > "$D/arms/L.env" <<'EOF'
MAGIC=9121
ARGS=--fibo-tf M15 --trend --money-tp 500 --risk-pct 2.0
EOF

systemctl --user daemon-reload
echo "installed units: xvfb99, mt5-term, fiboscalper@ (+ arms H, L)"
systemctl --user list-unit-files --no-pager --no-legend | grep -E 'xvfb99|mt5-term|fiboscalper'
