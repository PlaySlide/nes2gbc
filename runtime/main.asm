; nes2gbc Game Boy Color runtime skeleton.
DEF rKEY1 EQU $FF4D
DEF rLCDC EQU $FF40

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

    xor a
    ldh [rLCDC & $FF], a

    jp nes_reset

INCLUDE "io.asm"
INCLUDE "generated.asm"
