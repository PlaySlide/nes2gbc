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
    ld a, [nes_nmi_active]
    and a
    jr nz, .done
    ld a, $01
    ld [nes_host_vblank_pending], a
.done:
    pop af
    reti

Start:
    di
    ld sp, $FFFE

    ; Request CGB double-speed mode.
    ld a, $01
    ldh [rKEY1], a
    stop

    ; Canonical power-on state used by the recompiled 6502.
    xor a
    ld [nes_a], a
    ld [nes_x], a
    ld [nes_y], a
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
    ld [nes_current_code_bank], a
    ld [nes_dispatch_cache_valid], a

    ld a, $FD
    ld [nes_sp], a
    ld a, $24
    ld [nes_p], a
    ; Initial P=$24 has Z=0 and N=0.
    ld a, $01
    ld [nes_z_shadow], a
    xor a
    ld [nes_n_shadow], a

    call nes_generated_init
    call nes_video_init

    ; Use the real GBC VBlank interrupt only as a one-byte event latch. The
    ; translated NES interrupt is still delivered at compiler-selected safe points.
    xor a
    ldh [rIF], a
    ld a, $01
    ld [rIE], a
    ei
    jp nes_reset

INCLUDE "io.asm"
INCLUDE "cpu.asm"
INCLUDE "ppu.asm"
INCLUDE "video.asm"
INCLUDE "input.asm"
INCLUDE "generated.asm"
