; nes2gbc Game Boy Color runtime skeleton.
INCLUDE "hardware.inc"

SECTION "VBlank Vector", ROM0[$0040]
    jp nes_gbc_vblank_isr

SECTION "Header Entry", ROM0[$0100]
    nop
    jp Start

SECTION "Runtime", ROM0[$0150]
nes_gbc_vblank_isr:
    push af
    push bc
    push de
    push hl

    ; Flush virtual NES OAM exactly once at the start of host VBlank instead
    ; of blocking translated $4014 writes until VBlank arrives.
    ld a, [nes_oam_dirty]
    and a
    jr z, .oam_done
    xor a
    ld [nes_oam_dirty], a
    call nes_video_sync_oam
.oam_done:

    ld a, [nes_nmi_active]
    and a
    jr nz, .done
    ld a, $01
    ld [nes_host_vblank_pending], a
.done:
    pop hl
    pop de
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
    ld a, $01
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
