#!/bin/bash
# Pasang pelan kawalan CyberFIBO.
#
# Apa yang dipasang:
#   /opt/trading/bin/traderctl          (CLI + bot Telegram + notifier, satu fail)
#   /opt/trading/bin/mt5_login.sh|.py   (login broker ikut STDIN, argv bersih)
#   /opt/trading/.env                    (600, hanya kau)
#   ~/.config/systemd/user/trader-bot.service
#   ~/.config/systemd/user/trader-notify.service
#   /usr/local/bin/traderctl             (symlink, supaya boleh taip di mana-mana)
#
# Apa yang TIDAK disentuh: nginx, postgres, aplikasi lain, firewall, mana-mana port.
set -e

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
D=${TRADER_ROOT:-/opt/trading}
U="$HOME/.config/systemd/user"

echo "sumber : $SRC"
echo "sasaran: $D"

sudo mkdir -p "$D"/bin "$D"/state "$D"/logs "$D"/data "$D"/arms
sudo chown -R "$(id -un):$(id -gn)" "$D"
sudo chmod 755 "$D"

install -m 755 "$SRC/control/traderctl"    "$D/bin/traderctl"
install -m 644 "$SRC/control/mt5_login.py" "$D/bin/mt5_login.py"
install -m 755 "$SRC/control/mt5_login.sh" "$D/bin/mt5_login.sh"
sudo ln -sf "$D/bin/traderctl" /usr/local/bin/traderctl

if [ ! -f "$D/.env" ]; then
  if [ -f "$SRC/.env" ]; then
    install -m 600 "$SRC/.env" "$D/.env"
    echo "  .env disalin dari repo (600)"
  else
    install -m 600 "$SRC/.env.example" "$D/.env"
    echo "  ??  .env dicipta dari .env.example - ISI TG_TOKEN/TG_CHAT kemudian chmod 600"
  fi
else
  chmod 600 "$D/.env"
  echo "  .env sedia ada - dibiarkan"
fi

mkdir -p "$U"
install -m 644 "$SRC/control/systemd/trader-bot.service"    "$U/trader-bot.service"
install -m 644 "$SRC/control/systemd/trader-notify.service" "$U/trader-notify.service"

# penting: unit user mesti terus hidup selepas logout & selepas reboot
sudo loginctl enable-linger "$(id -un)"

systemctl --user daemon-reload
systemctl --user enable --now trader-bot.service trader-notify.service

echo
echo "=== status ==="
systemctl --user is-enabled trader-bot trader-notify | tr '\n' ' '; echo
systemctl --user is-active  trader-bot trader-notify | tr '\n' ' '; echo
echo
echo "seterusnya:"
echo "  traderctl tgcheck          # pastikan bot boleh hantar mesej kepada kau"
echo "  traderctl sim && traderctl status"
echo "  ? kemudian buka bot Telegram dan tekan START"
