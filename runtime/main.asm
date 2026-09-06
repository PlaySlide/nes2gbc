; nes2gbc Game Boy Color runtime skeleton.
INCLUDE "hardware.inc"

SECTION "VBlank Vector", ROM0[$0040]
    jp nes_gbc_vblank_isr

SECTION "STAT Vector", ROM0[$0048]
    jp nes_gbc_stat_isr

SECTION "Header Entry", ROM0[$0100]
    nop
    jp Start

SECTION "Runtime", ROM0[$0150]
nes_gbc_vblank_isr:
    push af
    push bc
    push de
    push hl

    ; A translated NES NMI may take more than one host GBC frame. Never publish
    ; partially updated NES video state while it is still running; staged OAM,
    ; palette, control, and scroll state are committed atomically on the first
    ; host VBlank after translated RTI clears nes_nmi_active.
    ld a, [nes_nmi_active]
    and a
    jp nz, .done

    ; Flush virtual NES OAM exactly once at the start of host VBlank.
    ; Normal $4014 DMA has already built the 160-byte GBC OAM shadow; direct
    ; $2004 writers fall back to building it here.
    ld a, [nes_oam_dirty]
    and a
    jr z, .oam_done
    xor a
    ld [nes_oam_dirty], a

    ldh a, [nes_oam_shadow_ready]
    and a
    jr nz, .oam_shadow_ready
    call nes_video_build_oam_shadow
.oam_shadow_ready:
    call nes_video_sync_oam
.oam_done:

    ldh a, [nes_palette_dirty]
    and a
    jr z, .palette_done
    xor a
    ldh [nes_palette_dirty], a
    call nes_video_sync_palette_shadow
.palette_done:

    ; Commit display-control and scroll state only on a host frame boundary.
    ; This prevents partial $2005 pairs / mid-scan PPUCTRL writes from tearing
    ; the entire GBC viewport.
    ldh a, [nes_ctrl_dirty]
    and a
    jr z, .ctrl_done
    xor a
    ldh [nes_ctrl_dirty], a
    call nes_video_update_ctrl
.ctrl_done:

    ldh a, [nes_scroll_dirty]
    and a
    jr z, .scroll_done
    xor a
    ldh [nes_scroll_dirty], a

    ; If the translated NES NMI produced two complete scroll pairs, preserve
    ; the first for the fixed top/HUD region and switch to the second with a
    ; one-shot GBC LYC interrupt. Otherwise use the normal single scroll.
    ldh a, [nes_split_active]
    and a
    jr z, .scroll_single

    ldh a, [nes_split_top_x]
    ld b, a
    ldh a, [nes_view_x]
    add b
    ldh [rSCX], a
    ldh a, [nes_split_top_y]
    ld b, a
    ldh a, [nes_view_y]
    add b
    ldh [rSCY], a

    ldh a, [nes_split_line]
    ldh [rLYC], a
    ldh a, [rSTAT]
    or $40
    ldh [rSTAT], a
    jr .scroll_done

.scroll_single:
    ; Disable any stale one-shot raster source and apply one coherent pair.
    ldh a, [rSTAT]
    and $BF
    ldh [rSTAT], a
    call nes_view_apply_scroll
.scroll_done:

    ; This host frame was presented from a completed NES state, so it may
    ; also become the next translated NES NMI event.
    ld a, $01
    ld [nes_host_vblank_pending], a
.done:
    pop hl
    pop de
    pop bc
    pop af
    reti

nes_gbc_stat_isr:
    push af
    push bc

    ; One-shot lower/playfield scroll for a captured two-state NES raster split.
    ldh a, [nes_split_bottom_x]
    ld b, a
    ldh a, [nes_view_x]
    add b
    ldh [rSCX], a

    ldh a, [nes_split_bottom_y]
    ld b, a
    ldh a, [nes_view_y]
    add b
    ldh [rSCY], a

    ; Disable the LYC source until the next VBlank arms another split.
    ldh a, [rSTAT]
    and $BF
    ldh [rSTAT], a

    pop bc
    pop af
    reti

Start:
    di
    ld sp, $D000

    ; Request CGB double-speed mode.
    ld a, $01
    ldh [rKEY1], a
    stop

    ; Match NES overlap ordering: in CGB mode, OPRI=0 gives priority by
    ; OAM index rather than DMG-style X-coordinate priority.
    xor a
    ldh [rOPRI], a

    ; Canonical power-on state used by the recompiled 6502.
    xor a
    ldh [nes_a], a
    ldh [nes_x], a
    ldh [nes_y], a
    ld [nes_ppu_status], a
    ld [nes_ppuctrl], a
    ld [nes_ppumask], a
    ld [nes_oamaddr], a
    ld [nes_ppu_scroll_x], a
    ld [nes_ppu_scroll_y], a
    ld [nes_ppu_addr_hi], a
    ld [nes_ppu_addr_lo], a
    ld [nes_ppu_latch], a
    ld [nes_ppu_read_buffer], a
    ld [nes_dac], a
    ld [nes_controller_strobe], a
    ld [nes_controller_shift], a
    ld [nes_host_vblank_pending], a
    ld [nes_nmi_active], a
    ld [nes_oam_dirty], a
    ld [nes_current_code_bank], a
    ld [nes_dispatch_cache_valid], a
    ld [nes_view_mode], a
    ldh [nes_view_x], a
    ldh [nes_view_y], a
    ld [nes_view_select_prev], a
    ldh [nes_reset_count], a
    ldh [nes_fault_hram], a
    ldh [nes_last_indirect_lo], a
    ldh [nes_last_indirect_hi], a
    ldh [nes_oam_shadow_ready], a
    ldh [nes_palette_dirty], a
    ldh [nes_scroll_dirty], a
    ldh [nes_ctrl_dirty], a
    ldh [nes_scroll_pair_count], a
    ldh [nes_split_active], a
    ldh [nes_split_top_x], a
    ldh [nes_split_top_y], a
    ldh [nes_split_bottom_x], a
    ldh [nes_split_bottom_y], a
    ld a, $20
    ldh [nes_split_line], a
    xor a

    ld hl, nes_gbc_palette_shadow
    ld b, $40
.clear_palette_shadow:
    ld [hli], a
    dec b
    jr nz, .clear_palette_shadow

    ld a, $FD
    ldh [nes_sp], a
    ld a, $24
    ldh [nes_p], a
    ; Initial P=$24 has C=0, Z=0, N=0.
    ld a, $01
    ldh [nes_z_shadow], a
    xor a
    ldh [nes_n_shadow], a
    ldh [nes_c_shadow], a

    call nes_generated_init
    call nes_video_init
    ; Start profiling at the translated NES reset, excluding GBC boot/setup work.
    call nes_profile_reset

    ; Use the real GBC VBlank interrupt only as a one-byte event latch. The
    ; translated NES interrupt is still delivered at compiler-selected safe points.
    xor a
    ldh [rIF], a
    ldh a, [rSTAT]
    and $BF
    ldh [rSTAT], a
    ld a, $03
    ld [rIE], a
    ei
    jp nes_reset

INCLUDE "io.asm"
INCLUDE "profile.asm"
INCLUDE "cpu.asm"
INCLUDE "ppu.asm"
INCLUDE "video.asm"
INCLUDE "input.asm"
INCLUDE "generated.asm"
