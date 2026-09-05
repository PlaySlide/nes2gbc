; nes2gbc Game Boy Color runtime skeleton.
; RGBDS syntax. This is intentionally tiny until the compiler emits code.

DEF rKEY1 EQU $FF4D
DEF rLCDC EQU $FF40
DEF rIE   EQU $FFFF

SECTION "VBlank Vector", ROM0[$0040]
    reti

SECTION "Header Entry", ROM0[$0100]
    nop
    jp Start

; RGBDS rgbfix fills/checks the rest of the cartridge header.
SECTION "Runtime", ROM0[$0150]
Start:
    di

    ; Request CGB double-speed mode. STOP performs the speed switch when KEY1 bit 0 is set.
    ld a, $01
    ldh [rKEY1 & $FF], a
    stop

    ; Keep LCD disabled until generated graphics state is ready.
    xor a
    ldh [rLCDC & $FF], a

.hang:
    halt
    jr .hang
