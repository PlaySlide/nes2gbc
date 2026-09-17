ROM ?=
MAX_BLOCKS ?=
TRACE ?= 0
PROFILE ?= 0
PROFILE_TRACE ?= 0
PEEPHOLE ?= 1
POSTPASS_THROUGH ?= all

.PHONY: help generate gbc test clean

help:
	@echo 'nes2gbc'
	@echo '  make gbc ROM="path/to/game.nes"'
	@echo '  make gbc ROM="path/to/game.nes" TRACE=1       # enable runtime breadcrumbs'
	@echo '  make gbc ROM="path/to/game.nes" PROFILE=1     # light runtime counters'
	@echo '  make gbc ROM="path/to/game.nes" PROFILE_TRACE=1 # expensive rolling block trace'
	@echo '  make gbc ROM="path/to/game.nes" MAX_BLOCKS=64   # optional development slice'
	@echo '  make gbc ROM="path/to/game.nes" PEEPHOLE=0      # disable generated-asm perf pass'
	@echo '  make gbc ROM="path/to/game.nes" POSTPASS_THROUGH=sprite0     # stop after sprite0 wait passes'
	@echo '  make gbc ROM="path/to/game.nes" POSTPASS_THROUGH=rts         # stop after RTS passes'
	@echo '  make gbc ROM="path/to/game.nes" POSTPASS_THROUGH=cache-xy-zp # add X/Y + hot-ZP caches only'
	@echo '  make gbc ROM="path/to/game.nes" POSTPASS_THROUGH=cache-a     # add A cache too'
	@echo '  make gbc ROM="path/to/game.nes" POSTPASS_THROUGH=cache       # add all cache passes'
	@echo '  make test'

generate:
	@test -n "$(ROM)" || (echo "ROM is required, e.g. make gbc ROM=game.nes" >&2; exit 2)
	@if [ -n "$(MAX_BLOCKS)" ]; then \
		cargo run -- "$(ROM)" --emit-asm runtime/generated.asm --max-blocks "$(MAX_BLOCKS)" $(if $(filter 1,$(TRACE)),--debug-trace,); \
	else \
		cargo run -- "$(ROM)" --emit-asm runtime/generated.asm $(if $(filter 1,$(TRACE)),--debug-trace,); \
	fi
	@if [ "$(PEEPHOLE)" = "1" ]; then \
		python3 tools/specialize_inline_dispatchers.py runtime/generated.asm "$(ROM)"; \
		python3 tools/peephole_generated.py runtime/generated.asm; \
		python3 tools/collapse_conditional_jp.py runtime/generated.asm; \
		python3 tools/tighten_stack_generated.py runtime/generated.asm; \
		python3 tools/shrink_compare_generated.py runtime/generated.asm; \
		python3 tools/hot_alu_generated.py runtime/generated.asm; \
		python3 tools/fast_oam_dma_generated.py runtime/generated.asm; \
		python3 tools/lazy_overflow_updates.py runtime/generated.asm; \
		python3 tools/fold_fixed_prg_reads.py runtime/generated.asm "$(ROM)"; \
		python3 tools/trim_indexed_ram_bus.py runtime/generated.asm; \
		if [ "$(TRACE)" != "1" ]; then python3 tools/mirror_indexed_prg_tables.py runtime/generated.asm "$(ROM)"; fi; \
		python3 tools/index_math_generated.py runtime/generated.asm; \
		python3 tools/fuse_page_aligned_cached_store.py runtime/generated.asm; \
		python3 tools/remove_index_flag_scaffolding.py runtime/generated.asm; \
		python3 tools/inline_ppustatus_generated.py runtime/generated.asm; \
		python3 tools/specialize_sprite0_poll.py runtime/generated.asm; \
		python3 tools/fuse_sprite0_branch.py runtime/generated.asm; \
		python3 tools/virtualize_sprite0_waits.py runtime/generated.asm; \
		if [ "$(POSTPASS_THROUGH)" != "sprite0" ]; then \
			python3 tools/dead_terminal_zn.py runtime/generated.asm; \
			python3 tools/dead_terminal_n.py runtime/generated.asm; \
			python3 tools/dead_terminal_carry_zn.py runtime/generated.asm; \
			python3 tools/dead_terminal_overflow_zn.py runtime/generated.asm; \
			python3 tools/native_leaf_calls.py runtime/generated.asm; \
			python3 tools/fast_leaf_rts_dispatch.py runtime/generated.asm; \
			python3 tools/fast_subroutine_rts_dispatch.py runtime/generated.asm; \
			python3 tools/defer_subroutine_rts_increment.py runtime/generated.asm; \
		fi; \
		if [ "$(POSTPASS_THROUGH)" = "cache-xy-zp" ] || [ "$(POSTPASS_THROUGH)" = "cache-a" ] || [ "$(POSTPASS_THROUGH)" = "cache" ] || [ "$(POSTPASS_THROUGH)" = "all" ]; then \
			python3 tools/cache_xy_in_blocks.py runtime/generated.asm; \
			python3 tools/cache_hot_zp_in_blocks.py runtime/generated.asm; \
		fi; \
		if [ "$(POSTPASS_THROUGH)" = "cache-a" ] || [ "$(POSTPASS_THROUGH)" = "cache" ] || [ "$(POSTPASS_THROUGH)" = "all" ]; then \
			python3 tools/cache_a_in_blocks.py runtime/generated.asm; \
		fi; \
		if [ "$(POSTPASS_THROUGH)" = "cache" ] || [ "$(POSTPASS_THROUGH)" = "all" ]; then \
			python3 tools/cache_de_in_blocks.py runtime/generated.asm; \
		fi; \
		if [ "$(POSTPASS_THROUGH)" = "all" ]; then \
			python3 tools/elide_nmi_internal_polls.py runtime/generated.asm; \
			python3 tools/direct_nmi_dispatch.py runtime/generated.asm; \
			python3 tools/fast_rti_dispatch.py runtime/generated.asm; \
			python3 tools/guard_indirect_dispatch.py runtime/generated.asm "$(ROM)"; \
			python3 tools/fast_code_bank_switch.py runtime/generated.asm; \
			python3 tools/widen_generated_jumps.py runtime/generated.asm; \
		fi; \
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
