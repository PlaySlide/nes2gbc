ROM ?=
MAX_BLOCKS ?=
TRACE ?= 0
PROFILE ?= 0

.PHONY: help generate gbc test clean

help:
	@echo 'nes2gbc'
	@echo '  make gbc ROM="path/to/game.nes"'
	@echo '  make gbc ROM="path/to/game.nes" TRACE=1       # enable runtime breadcrumbs'
	@echo '  make gbc ROM="path/to/game.nes" PROFILE=1     # count hot runtime paths'
	@echo '  make gbc ROM="path/to/game.nes" MAX_BLOCKS=64   # optional development slice'
	@echo '  make test'

generate:
	@test -n "$(ROM)" || (echo "ROM is required, e.g. make gbc ROM=game.nes" >&2; exit 2)
	@if [ -n "$(MAX_BLOCKS)" ]; then \
		cargo run -- "$(ROM)" --emit-asm runtime/generated.asm --max-blocks "$(MAX_BLOCKS)" $(if $(filter 1,$(TRACE)),--debug-trace,); \
	else \
		cargo run -- "$(ROM)" --emit-asm runtime/generated.asm $(if $(filter 1,$(TRACE)),--debug-trace,); \
	fi

gbc: generate
	$(MAKE) -C runtime TRACE="$(TRACE)" PROFILE="$(PROFILE)"

test:
	cargo test --all-targets
	cargo check --all-targets

clean:
	cargo clean
	$(MAKE) -C runtime clean
	rm -f runtime/generated.prg.bin runtime/generated.chr.bin runtime/generated.chr.gbc.bin
