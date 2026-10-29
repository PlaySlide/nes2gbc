ROM ?=
MAX_BLOCKS ?=
TRACE ?= 0
PROFILE ?= 0
PROFILE_TRACE ?= 0
PEEPHOLE ?= 1
# Compiler register allocation level (src/state_superblock.rs); 0 = previous emission.
REGALLOC ?= 3
# NES APU/sound emulation (runtime/apu.asm, docs/APU.md). 0 = silent (opt-out),
# no APU cost; 1 (default) = APU register writes/$4015 reads drive CGB sound. Set it on
# `make generate`/`make gbc`; it is recorded in runtime/generated_config.inc.
APU ?= 1
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
	@if [ "$${NES2GBC_ISR_BANK_FIX:-1}" != "0" ]; then echo 'DEF NES2GBC_ISR_BANK_FIX EQU 1 ; VBlank ISR restores the ROM bank of the interrupted runtime window (opt out: NES2GBC_ISR_BANK_FIX=0)' >> runtime/generated_config.inc; fi
	@if [ "$${NES2GBC_SINGLE_HSTITCH:-1}" != "0" ]; then echo 'DEF NES2GBC_SINGLE_HSTITCH EQU 1 ; stitch full-screen vertical-mirroring horizontal scroll across the NT seam (opt out: NES2GBC_SINGLE_HSTITCH=0)' >> runtime/generated_config.inc; fi
	@if [ "$${NES2GBC_STITCH_SHADOW_SYNC:-1}" != "0" ]; then echo 'DEF NES2GBC_STITCH_SHADOW_SYNC EQU 1 ; resync the published-nametable shadow when a single-scroll stitch engages (opt out: NES2GBC_STITCH_SHADOW_SYNC=0)' >> runtime/generated_config.inc; fi
	@if [ "$${NES2GBC_SPLIT_YWRAP:-1}" != "0" ]; then echo 'DEF NES2GBC_SPLIT_YWRAP EQU 1 ; apply the NES 240-line nametable wrap to the raster-split playfield SCY (opt out: NES2GBC_SPLIT_YWRAP=0)' >> runtime/generated_config.inc; fi
	@if [ "$${NES2GBC_SPLIT_TOP_YWRAP:-1}" != "0" ]; then echo 'DEF NES2GBC_SPLIT_TOP_YWRAP EQU 1 ; show the HUD past the NES 240-line wrap when most split-top lines lie beyond it (opt out: NES2GBC_SPLIT_TOP_YWRAP=0)' >> runtime/generated_config.inc; fi
	@if [ "$${NES2GBC_HALT_IDLE:-1}" != "0" ]; then echo 'DEF NES2GBC_HALT_IDLE EQU 1 ; idle-spin HALT/early NMI (opt out: NES2GBC_HALT_IDLE=0)' >> runtime/generated_config.inc; fi
	@if [ "$(CATCHUP)" = "1" ]; then echo 'DEF NES2GBC_CATCHUP EQU 1 ; make generate CATCHUP=1' >> runtime/generated_config.inc; \
		echo 'DEF NES2GBC_CATCHUP_MAX EQU $(CATCHUP_MAX)' >> runtime/generated_config.inc; fi
	@if [ -n "$(MAX_BLOCKS)" ]; then \
		NES2GBC_REGALLOC="$(REGALLOC)" NES2GBC_APU="$(APU)" NES2GBC_RTS_PROFILE="$(RTS_PROFILE)" cargo run -- "$(ROM)" --emit-asm runtime/generated.asm --max-blocks "$(MAX_BLOCKS)" $(if $(filter 1,$(TRACE)),--debug-trace,); \
	else \
		NES2GBC_REGALLOC="$(REGALLOC)" NES2GBC_APU="$(APU)" NES2GBC_RTS_PROFILE="$(RTS_PROFILE)" cargo run -- "$(ROM)" --emit-asm runtime/generated.asm $(if $(filter 1,$(TRACE)),--debug-trace,); \
	fi
	@if [ "$${NES2GBC_ENTRY_SP_EARLY:-1}" = "0" ]; then echo 'DEF NES2GBC_ENTRY_SP_EARLY_OFF EQU 1 ; restore the old NMI delivery order (opt-out for NES2GBC_ENTRY_SP_EARLY=0)' >> runtime/generated_config.inc; fi
	@n=$$(sed -n 's/^; NES2GBC_ADDR_SCROLL \([0-9]*\)$$/\1/p' runtime/generated.asm | head -1); if [ -n "$$n" ]; then echo "DEF NES2GBC_ADDR_SCROLL EQU $$n ; mid-frame PPUADDR scroll, HUD lines (data/addr_scroll.txt)" >> runtime/generated_config.inc; fi
	@n=$$(sed -n 's/^; NES2GBC_HUD_BOTTOM \([0-9]*\)$$/\1/p' runtime/generated.asm | head -1); if [ -n "$$n" ]; then echo "DEF NES2GBC_HUD_BOTTOM EQU $$n ; bottom HUD band top line (data/hud_bottom.txt)" >> runtime/generated_config.inc; fi
	@if grep -q '^; NES2GBC_ZAPPER 1' runtime/generated.asm; then echo 'DEF NES2GBC_ZAPPER EQU 1 ; Zapper on port 2, A = trigger (data/zapper.txt; opt out: NES2GBC_NO_ZAPPER=1)' >> runtime/generated_config.inc; fi
	@if grep -q '^; NES2GBC_FIXED_CAMERA 1' runtime/generated.asm; then echo 'DEF NES2GBC_FIXED_CAMERA EQU 1 ; data-driven fixed follow crop (hint mode C; opt out: NES2GBC_NO_FIXED_CAMERA=1)' >> runtime/generated_config.inc; fi
	@if grep -q '^; NES2GBC_NMI_NEST 1' runtime/generated.asm; then echo 'DEF NES2GBC_NMI_NEST EQU 1 ; handler guarded by a re-entrancy flag: deliver a nested NMI when it spans several host VBlanks (opt out: NES2GBC_NMI_NEST=0)' >> runtime/generated_config.inc; fi
	@if grep -q '^; NES2GBC_NMI_ABANDON 1' runtime/generated.asm; then echo 'DEF NES2GBC_NMI_ABANDON EQU 1 ; NMI handler may reset SP and never RTI (opt out: NES2GBC_NMI_ABANDON=0)' >> runtime/generated_config.inc; fi
	@if grep -q '^; NES2GBC_QUIRK_LATCH 1' runtime/generated.asm; then echo 'DEF NES2GBC_QUIRK_LATCH EQU 1 ; vblank-flag latch (data/ppu_quirks.txt; opt out: NES2GBC_NO_PPU_QUIRKS=1)' >> runtime/generated_config.inc; fi
	@if grep -q '^; NES2GBC_QUIRK_DEBOUNCE 1' runtime/generated.asm; then echo 'DEF NES2GBC_QUIRK_DEBOUNCE EQU 1 ; 3-frame BG-pattern-bank debounce (data/ppu_quirks.txt; opt out: NES2GBC_NO_PPU_QUIRKS=1)' >> runtime/generated_config.inc; fi
	@if grep -q '^; NES2GBC_QUIRK_OBJHOLD 1' runtime/generated.asm; then echo 'DEF NES2GBC_QUIRK_OBJHOLD EQU 1 ; keep OBJ on while an NMI handler runs (data/ppu_quirks.txt; opt out: NES2GBC_NO_PPU_QUIRKS=1)' >> runtime/generated_config.inc; fi
	@if grep -q '^; NES2GBC_NMI_HOLD 1' runtime/generated.asm; then printf '%s\n' 'DEF NES2GBC_NMI_HOLD EQU 1 ; hold a vblank NMI while the main thread is mid-work (data/nmi_hold.txt; opt out: NES2GBC_NO_NMI_HOLD=1)' 'DEF NES2GBC_NMI_HOLD_FRAMES EQU 4' >> runtime/generated_config.inc; fi
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
			if ! grep -q '^; NES2GBC_NMI_NEST 1' runtime/generated.asm; then python3 tools/elide_nmi_internal_polls.py runtime/generated.asm; fi; \
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
			python3 tools/banked_static_prg_reads.py runtime/generated.asm "$(ROM)"; \
			python3 tools/inline_prg_reads.py runtime/generated.asm; \
			python3 tools/chain_indexed_hl.py runtime/generated.asm; \
			python3 tools/dead_overflow.py runtime/generated.asm; \
			python3 tools/dead_overflow_global.py runtime/generated.asm; \
			python3 tools/fuse_mmc1_serial_writes.py runtime/generated.asm; \
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
			python3 tools/host_z_branch.py runtime/generated.asm; \
			python3 tools/skip_jp_collapse.py runtime/generated.asm; \
			python3 tools/small_peepholes.py runtime/generated.asm; \
			python3 tools/flag_dead_peepholes.py runtime/generated.asm; \
			if [ "$$NES2GBC_FINAL_DEAD_HRAM" != "0" ]; then \
				python3 tools/dead_hram_state_global.py runtime/generated.asm; \
				python3 tools/dead_overflow_global.py runtime/generated.asm; \
				python3 tools/dead_reg_writes.py runtime/generated.asm; \
			fi; \
			python3 tools/rts_raw_guard_compare.py runtime/generated.asm; \
			python3 tools/tighten_ram_test.py runtime/generated.asm; \
			python3 tools/redundant_zero_test.py runtime/generated.asm; \
			python3 tools/page_aligned_index.py runtime/generated.asm; \
		fi; \
		# TRACE and partial POSTPASS_THROUGH builds also expand generated blocks. \
		# Normalize short/long NES-label jumps after the final selected pass so \
		# debug/pass-isolation builds cannot leave an out-of-range JR behind. \
		if [ "$(POSTPASS_THROUGH)" = "all" ]; then python3 tools/outline_nmi_polls.py runtime/generated.asm; fi; \
		if [ "$${NES2GBC_HALT_IDLE:-1}" != "0" ]; then python3 tools/halt_idle_loops.py runtime/generated.asm; fi; \
		python3 tools/widen_generated_jumps.py runtime/generated.asm; \
		if [ "$(POSTPASS_THROUGH)" = "all" ]; then python3 tools/selfloop_promote.py runtime/generated.asm; fi; \
		if [ "$(POSTPASS_THROUGH)" = "all" ] && [ "$${NES2GBC_KEEP_BC_READS:-1}" != "0" ]; then python3 tools/keep_bc_reads.py runtime/generated.asm; fi; \
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
