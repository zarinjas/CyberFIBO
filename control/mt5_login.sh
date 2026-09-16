#!/bin/bash
# MT5 login helper. Credentials arrive on STDIN and are piped straight into the
# Wine python - so the password never appears in argv (`ps`) or the environment
# (`/proc/*/environ`). Only a single line of JSON is printed back.
set -e

export WINEPREFIX=/opt/trading/wineprefix
export WINEDEBUG=-all
export DISPLAY=:99
export WINEDLLOVERRIDES="mscoree,mshtml="

if ! pgrep -f "Xvfb :99" >/dev/null; then
  nohup Xvfb :99 -screen 0 1280x800x24 -nolisten tcp >/opt/trading/logs/xvfb.log 2>&1 &
  sleep 3
fi

# make sure the terminal is up before asking it to log in
if ! pgrep -f terminal64.exe >/dev/null; then
  nohup wine "C:/Program Files/MetaTrader 5/terminal64.exe" /portable \
    >/opt/trading/logs/mt5-term.log 2>&1 &
  sleep 45
fi

wine "C:/Python311/python.exe" "Z:/opt/trading/bin/mt5_login.py" 2>/dev/null \
  | tr -d '\r' | grep -m1 '^MT5LOGIN ' | sed 's/^MT5LOGIN //'
