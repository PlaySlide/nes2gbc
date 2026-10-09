ROM ?=
MAX_BLOCKS ?=
TRACE ?= 0
PROFILE ?= 0
PROFILE_TRACE ?= 0
PEEPHOLE ?= 1
# Compiler register allocation level (src/state_superblock.rs); 0 = previous emission.
REGALLOC ?= 3
# NES APU/sound emulation (runtime/apu.asm, docs/APU.md). 0 (default) = silent,
# no APU cost; 1 = APU register writes/$4015 reads drive CGB sound. Set it on
# `make generate`/`make gbc`; it is recorded in runtime/generated_config.inc.
APU ?= 0
# Multi-frame catch-up (runtime pacing): 1 = bank up to CATCHUP_MAX host VBlanks
# that elapsed while a translated NMI ran, so following short frames start
# their NMI without waiting for the next VBlank. 0 (default) = previous
# single-credit pacing, byte-identical builds. Recorded in generated_config.inc.
CATCHUP ?= 0
CATCHUP_MAX ?= 3
POSTPASS_THROUGH ?= all
# Profile-guided translated-code bank packing (tools/bench/bank_profile.py).
# Defaults to profiles/<rom name>.bankprof when that file exists; BANK_PROFILE=
# (empty) or a missing file keeps the static whole-bank packing.
BANK_PROFILE ?= profiles/$(basename $(notdir $(ROM))).bankprof
# Block-entry profile (tools/bench/rts_profile.py) ordering guarded RTS returns;
# RTS_PROFILE= (empty) or a missing file keeps static JSR-site ordering.
RTS_PROFILE ?= profiles/$(basename $(notdir $(ROM))).rtsprof
# Per-RTS-site return edges (tools/bench/rts_edge_profile.py) order the compare chains.
RTS_EDGE_PROFILE ?= profiles/$(basename $(notdir $(ROM))).rtsedge
# 1 = keep every emitter bank unmerged (layout used to record a bank profile).
REPACK_IDENTITY ?= 0
# APU=1 only: audio host-speed compensation for testing under 2x/4x fast-forward.
APU_TEST_SPEED ?= 1

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
	@echo '  make gbc ROM="path/to/game.nes" APU=1         # enable NES sound (APU emulation)'
	@echo '  make gbc ROM="path/to/game.nes" CATCHUP=1     # multi-frame pacing catch-up (CATCHUP_MAX=3)'
	@echo '  make gbc ROM="path/to/game.nes" APU=1 APU_TEST_SPEED=2 # compensate audio for mGBA 2x fast-forward'
	@echo '  make test'

generate:
	@test -n "$(ROM)" || (echo "ROM is required, e.g. make gbc ROM=game.nes" >&2; exit 2)
	@if [ "$(APU)" = "1" ]; then echo 'DEF NES2GBC_APU EQU 1 ; make generate APU=1' > runtime/generated_config.inc; \
	else echo '; make generate APU=0: NES APU emulation disabled' > runtime/generated_config.inc; fi
	@if [ "$(CATCHUP)" = "1" ]; then echo 'DEF NES2GBC_CATCHUP EQU 1 ; make generate CATCHUP=1' >> runtime/generated_config.inc; \
		echo 'DEF NES2GBC_CATCHUP_MAX EQU $(CATCHUP_MAX)' >> runtime/generated_config.inc; fi
	@if [ -n "$(MAX_BLOCKS)" ]; then \
		NES2GBC_REGALLOC="$(REGALLOC)" NES2GBC_APU="$(APU)" cargo run -- "$(ROM)" --emit-asm runtime/generated.asm --max-blocks "$(MAX_BLOCKS)" $(if $(filter 1,$(TRACE)),--debug-trace,); \
	else \
		NES2GBC_REGALLOC="$(REGALLOC)" NES2GBC_APU="$(APU)" cargo run -- "$(ROM)" --emit-asm runtime/generated.asm $(if $(filter 1,$(TRACE)),--debug-trace,); \
	fi
	@if [ "$(PEEPHOLE)" = "1" ]; then \
		python3 tools/specialize_inline_dispatchers.py runtime/generated.asm "$(ROM)"; \
		python3 tools/peephole_generated.py runtime/generated.asm; \
		python3 tools/collapse_conditional_jp.py runtime/generated.asm; \
		python3 tools/tighten_stack_generated.py runtime/generated.asm; \
		python3 tools/shrink_compare_generated.py runtime/generated.asm; \
		python3 tools/hot_alu_generated.py runtime/generated.asm; \
		python3 tools/fast_oam_dma_generated.py runtime/generated.asm; \
		python3 tools/route_ppu_write_data.py runtime/generated.asm runtime/generated_config.inc; \
		python3 tools/lazy_overflow_updates.py runtime/generated.asm; \
		python3 tools/fold_fixed_prg_reads.py runtime/generated.asm "$(ROM)"; \
		python3 tools/trim_indexed_ram_bus.py runtime/generated.asm; \
		if [ "$(TRACE)" != "1" ]; then python3 tools/mirror_indexed_prg_tables.py runtime/generated.asm "$(ROM)"; fi; \
		python3 tools/index_math_generated.py runtime/generated.asm; \
		python3 tools/fuse_index_branch_value.py runtime/generated.asm; \
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
			python3 tools/fast_leaf_rts_dispatch.py runtime/generated.asm --rts-profile "$(RTS_PROFILE)"; \
			python3 tools/fast_subroutine_rts_dispatch_inline.py runtime/generated.asm --max-returns 8 --bank-budget 2400 --max-rts-per-bank 40 --rts-profile "$(RTS_PROFILE)"; \
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
			if [ "$(TRACE)" != "1" ]; then python3 tools/mirror_indexed_prg_tables.py runtime/generated.asm "$(ROM)"; fi; \
			python3 tools/fuse_index_branch_value.py runtime/generated.asm; \
			python3 tools/elide_nmi_internal_polls.py runtime/generated.asm; \
			python3 tools/direct_nmi_dispatch.py runtime/generated.asm; \
			python3 tools/fast_rti_dispatch.py runtime/generated.asm; \
			python3 tools/guard_indirect_dispatch.py runtime/generated.asm "$(ROM)"; \
			python3 tools/banked_direct_transfers.py runtime/generated.asm; \
			python3 tools/fast_code_bank_switch.py runtime/generated.asm; \
			python3 tools/fast_fill_loops.py runtime/generated.asm; \
			python3 tools/native_joypad_loops.py runtime/generated.asm "$(ROM)"; \
			python3 tools/native_blockbuf_collision.py runtime/generated.asm "$(ROM)"; \
			python3 tools/native_offscreen_bits.py runtime/generated.asm "$(ROM)"; \
			python3 tools/native_draw_sprite_object.py runtime/generated.asm "$(ROM)"; \
			python3 tools/native_multibyte_compare_copy.py runtime/generated.asm "$(ROM)"; \
			python3 tools/native_small_loops.py runtime/generated.asm "$(ROM)"; \
			python3 tools/native_enemy_parser.py runtime/generated.asm "$(ROM)"; \
			python3 tools/native_bounding_box.py runtime/generated.asm "$(ROM)"; \
			python3 tools/native_vram_run.py runtime/generated.asm "$(ROM)"; \
			python3 tools/native_metatile_column.py runtime/generated.asm "$(ROM)"; \
			python3 tools/native_relative_xy_leaf.py runtime/generated.asm "$(ROM)"; \
			python3 tools/native_tiny_leaves.py runtime/generated.asm "$(ROM)"; \
			python3 tools/repack_code_banks_final.py runtime/generated.asm --profile "$(PROFILE)" --profile-trace "$(PROFILE_TRACE)" --identity "$(REPACK_IDENTITY)" --bank-profile "$(BANK_PROFILE)"; \
			python3 tools/dead_hram_state_global.py runtime/generated.asm; \
			python3 tools/fast_nonram_reads.py runtime/generated.asm; \
			python3 tools/inline_prg_reads.py runtime/generated.asm; \
			python3 tools/chain_indexed_hl.py runtime/generated.asm; \
			python3 tools/dead_overflow.py runtime/generated.asm; \
			python3 tools/final_peephole.py runtime/generated.asm; \
			python3 tools/cheap_carry_materialize.py runtime/generated.asm; \
			python3 tools/sbc_carry_capture.py runtime/generated.asm; \
			python3 tools/dead_a_reload.py runtime/generated.asm; \
			python3 tools/store_reload.py runtime/generated.asm; \
			python3 tools/dead_af_compute.py runtime/generated.asm; \
			python3 tools/reg_copy_prop.py runtime/generated.asm; \
			python3 tools/push_af_temp.py runtime/generated.asm; \
			python3 tools/alu_imm_fold.py runtime/generated.asm; \
			python3 tools/dead_reg_writes.py runtime/generated.asm; \
			python3 tools/reg_copy_prop.py runtime/generated.asm; \
			python3 tools/push_af_temp.py runtime/generated.asm; \
			python3 tools/dead_reg_writes.py runtime/generated.asm; \
			python3 tools/sec_sbc_to_sub.py runtime/generated.asm; \
			python3 tools/rts_chain_reorder.py runtime/generated.asm --edge-profile "$(RTS_EDGE_PROFILE)"; \
			python3 tools/rts_compare_first.py runtime/generated.asm --rts-profile "$(RTS_PROFILE)" --edge-profile "$(RTS_EDGE_PROFILE)"; \
			python3 tools/native_dk_box_collision.py runtime/generated.asm "$(ROM)"; \
			python3 tools/native_move_object_h.py runtime/generated.asm "$(ROM)"; \
			python3 tools/native_dk_sprite_loops.py runtime/generated.asm "$(ROM)"; \
			python3 tools/fallthrough_layout.py runtime/generated.asm; \
			python3 tools/thread_adapter_jumps.py runtime/generated.asm; \
		fi; \
		# TRACE and partial POSTPASS_THROUGH builds also expand generated blocks. \
		# Normalize short/long NES-label jumps after the final selected pass so \
		# debug/pass-isolation builds cannot leave an out-of-range JR behind. \
		python3 tools/widen_generated_jumps.py runtime/generated.asm; \
	fi

gbc: generate
	# Runtime sources and generated.asm change frequently across mapper/perf branches.
	# Reassemble from scratch so a stale runtime.o can never be linked against a
	# newly-generated cartridge image after a branch switch/reset.
	$(MAKE) -C runtime clean
	$(MAKE) -C runtime TRACE="$(TRACE)" PROFILE="$(PROFILE)" PROFILE_TRACE="$(PROFILE_TRACE)" APU_TEST_SPEED="$(APU_TEST_SPEED)"

test:
	cargo test --all-targets
	cargo check --all-targets

clean:
	cargo clean
	$(MAKE) -C runtime clean
	rm -f runtime/generated.prg.bin runtime/generated.chr.bin runtime/generated.chr.gbc.bin
