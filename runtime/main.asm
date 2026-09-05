; nes2gbc Game Boy Color runtime skeleton.
DEF rKEY1 EQU $FF4D

SECTION "VBlank Vector", ROM0[$0040]
    reti

SECTION "Header Entry", ROM0[$0100]
    nop
    jp Start

SECTION "Runtime", ROM0[$0150]
Start:
    di

    ; Request CGB double-speed mode.
    ld a, $01
    ldh [rKEY1 & $FF], a
    stop

    ; Canonical power-on state used by the recompiled 6502.
    xor a
    ld [nes_a], a
    ld [nes_x], a
    ld [nes_y], a
    ld [nes_ppu_status], a
    ld [nes_ppuctrl], a
    ld [nes_ppumask], a
    ld [nes_dac], a

    ld a, $FD
    ld [nes_sp], a
    ld a, $24
    ld [nes_p], a

    jp nes_reset

INCLUDE "io.asm"
INCLUDE "cpu.asm"
INCLUDE "generated.asm"
