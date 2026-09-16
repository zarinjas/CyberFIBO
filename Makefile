# CyberFIBO — perintah pembangunan / deploy
# Guna: make <target>
.PHONY: help test test-verbose ctl status why sim syntax install-control uninstall-control health

help:
	@echo "make test              - jalankan suite kanonik (bot/tests)"
	@echo "make syntax            - semak sintaks semua fail python"
	@echo "make ctl ARGS=status   - jalankan traderctl dari repo"
	@echo "make sim               - tulis state tiruan (uji UI tanpa MT5)"
	@echo "make install-control   - pasang traderctl + unit systemd + .env"
	@echo "make uninstall-control - buang unit + traderctl"
	@echo "make health            - sahkan aplikasi lain tidak tergugat"

test:
	cd bot && python -m pytest -q

test-verbose:
	cd bot && python -m pytest -v

syntax:
	python -c "import ast,glob;[ast.parse(open(f,encoding='utf-8').read()) for f in glob.glob('bot/*.py')+glob.glob('control/*.py')+['control/traderctl']+glob.glob('studies/*.py')];print('syntax OK')"

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
