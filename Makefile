.PHONY: iso docker-iso clean test

iso:
	bash ./build/build-iso.sh

docker-iso:
	bash ./build/docker-build.sh

test:
	python3 tests/test_daemons.py
	python3 tests/test_assistant.py
	python3 -c 'from pathlib import Path; files=list(Path("overlay/usr/libexec/aether").glob("*.py")); [compile(p.read_text(), str(p), "exec") for p in files]; print(f"Compiled {len(files)} source files")'
	bash -n build/build-iso.sh build/install-local.sh build/install-runtime.sh build/docker-build.sh build/hooks/*.hook.chroot overlay/usr/bin/*
	sh -n overlay/etc/profile.d/aetheros-session.sh
	python3 -c 'import json; json.load(open("overlay/etc/xdg/waybar/config")); print("waybar config: valid JSON")'

clean:
	@if [ -d build/live-build ] && command -v lb >/dev/null 2>&1; then cd build/live-build && sudo lb clean --purge; else echo "No live-build workspace to clean"; fi
