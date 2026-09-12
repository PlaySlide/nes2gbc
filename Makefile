ROM ?=
MAX_BLOCKS ?=
TRACE ?= 0
PROFILE ?= 0
PROFILE_TRACE ?= 0
PEEPHOLE ?= 1

.PHONY: help generate gbc test clean

help:
	@echo 'nes2gbc'
	@echo '  make gbc ROM="path/to/game.nes"'
	@echo '  make gbc ROM="path/to/game.nes" TRACE=1       # enable runtime breadcrumbs'
	@echo '  make gbc ROM="path/to/game.nes" PROFILE=1     # light runtime counters'
	@echo '  make gbc ROM="path/to/game.nes" PROFILE_TRACE=1 # expensive rolling block trace'
	@echo '  make gbc ROM="path/to/game.nes" MAX_BLOCKS=64   # optional development slice'
	@echo '  make gbc ROM="path/to/game.nes" PEEPHOLE=0      # disable generated-asm perf pass'
	@echo '  make test'

generate:
	@test -n "$(ROM)" || (echo "ROM is required, e.g. make gbc ROM=game.nes" >&2; exit 2)
	@if [ -n "$(MAX_BLOCKS)" ]; then \
		cargo run -- "$(ROM)" --emit-asm runtime/generated.asm --max-blocks "$(MAX_BLOCKS)" $(if $(filter 1,$(TRACE)),--debug-trace,); \
	else \
		cargo run -- "$(ROM)" --emit-asm runtime/generated.asm $(if $(filter 1,$(TRACE)),--debug-trace,); \
	fi
	@if [ "$(PEEPHOLE)" = "1" ]; then \
		python3 tools/peephole_generated.py runtime/generated.asm; \
		python3 tools/tighten_stack_generated.py runtime/generated.asm; \
		python3 tools/shrink_compare_generated.py runtime/generated.asm; \
		python3 tools/hot_alu_generated.py runtime/generated.asm; \
		python3 tools/fold_fixed_prg_reads.py runtime/generated.asm "$(ROM)"; \
		python3 tools/trim_indexed_ram_bus.py runtime/generated.asm; \
		if [ "$(TRACE)" != "1" ]; then python3 tools/mirror_indexed_prg_tables.py runtime/generated.asm "$(ROM)"; fi; \
		python3 tools/index_math_generated.py runtime/generated.asm; \
		python3 tools/inline_ppustatus_generated.py runtime/generated.asm; \
		python3 tools/specialize_sprite0_poll.py runtime/generated.asm; \
		python3 tools/fuse_sprite0_branch.py runtime/generated.asm; \
		python3 tools/dead_terminal_zn.py runtime/generated.asm; \
		python3 tools/fuse_compare_carry_branch.py runtime/generated.asm; \
		python3 tools/fast_leaf_rts_dispatch.py runtime/generated.asm; \
		python3 tools/fast_subroutine_rts_dispatch.py runtime/generated.asm; \
		python3 tools/cache_xy_in_blocks.py runtime/generated.asm; \
		python3 tools/widen_generated_jumps.py runtime/generated.asm; \
	fi

gbc: generate
	$(MAKE) -C runtime TRACE="$(TRACE)" PROFILE="$(PROFILE)" PROFILE_TRACE="$(PROFILE_TRACE)"

test:
	cargo test --all-targets
	cargo check --all-targets

clean:
	cargo clean
	$(MAKE) -C runtime clean
	rm -f runtime/generated.prg.bin runtime/generated.chr.bin runtime/generated.chr.gbc.bin
