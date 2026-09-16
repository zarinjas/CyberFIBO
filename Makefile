# CyberFIBO — perintah pembangunan / deploy
# Guna: make <target>
# Verifikasi sebenar ada dalam tools/check.py supaya ia jalan di Windows juga
# (di mana `make` mungkin tiada):  python tools/check.py
.PHONY: help check test test-verbose syntax ctl status why sim install-control uninstall-control health

help:
	@echo "make check             - SEMUA verifikasi (sintaks + pytest + paparan)"
	@echo "make test              - alias untuk check"
	@echo "make test-verbose      - pytest dengan butiran"
	@echo "make ctl ARGS=status   - jalankan traderctl dari repo"
	@echo "make sim               - tulis state tiruan (uji UI tanpa MT5)"
	@echo "make install-control   - pasang traderctl + unit systemd + .env"
	@echo "make uninstall-control - buang unit + traderctl"
	@echo "make health            - sahkan aplikasi lain tidak tergugat"

check test syntax:
	python tools/check.py

test-verbose:
	cd bot && python -m pytest -v

ctl:
	python control/traderctl $(ARGS)

sim:
	python control/traderctl sim && python control/traderctl status

status:
	python control/traderctl status

why:
	python control/traderctl why

install-control:
	bash control/install_control.sh

uninstall-control:
	systemctl --user disable --now trader-bot trader-notify 2>/dev/null || true
	rm -f $(HOME)/.config/systemd/user/trader-bot.service $(HOME)/.config/systemd/user/trader-notify.service
	systemctl --user daemon-reload
	rm -f /usr/local/bin/traderctl

# buktikan beban trading tidak menjejaskan aplikasi lain
health:
	@echo "--- servis lain ---"
	@systemctl is-active nginx mykiznext postgresql@18-main || true
	@echo "--- restart lain (mesti 0) ---"
	@systemctl show mykiznext -p NRestarts --value || true
	@echo "--- tekanan memori (mesti ~0.00) ---"
	@cat /proc/pressure/memory | head -1
	@echo "--- OOM 1 jam terakhir (mesti 0) ---"
	@sudo journalctl -k --since '1 hour ago' --no-pager 2>/dev/null | grep -ci 'out of memory' || true
	@echo "--- unit trading ---"
	@systemctl --user is-active trader-bot trader-notify 2>/dev/null || true
