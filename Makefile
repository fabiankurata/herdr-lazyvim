.PHONY: audit test

audit:
	bash scripts/check-public.sh

test:
	bash tests/plugin_test.sh
	bash -n scripts/setup-macos.sh
	bash scripts/setup-macos.sh plan >/dev/null
	nvim --headless -u NONE -c "luafile tests/nvim_smoke.lua"
