; NES controller port 1 mapped to the Game Boy Color joypad.

SECTION "NES input helpers", ROM0

nes_controller_latch:
    ; Action buttons: GBC A/B/Select/Start map directly to NES bits 0-3.
    ld a, $10
    ldh [rP1], a
    ldh a, [rP1]
    cpl
    and $0F
    ld b, a

    ; Directions: GBC R,L,U,D -> NES bits 7,6,4,5.
    ld a, $20
    ldh [rP1], a
    ldh a, [rP1]
    cpl
    and $0F
    ld c, a

    bit 2, c
    jr z, .no_up
    ld a, b
    or $10
    ld b, a
.no_up:
    bit 3, c
    jr z, .no_down
    ld a, b
    or $20
    ld b, a
.no_down:
    bit 1, c
    jr z, .no_left
    ld a, b
    or $40
    ld b, a
.no_left:
    bit 0, c
    jr z, .no_right
    ld a, b
    or $80
    ld b, a
.no_right:
    ld a, b
    ld [nes_controller_shift], a
    ret

; Input A = value written to $4016.
nes_controller_write:
    and $01
    ld b, a
    ld a, [nes_controller_strobe]
    ld c, a
    ld a, b
    ld [nes_controller_strobe], a

    ; Latch on strobe high and again on the falling edge.
    and a
    jp nz, nes_controller_latch
    ld a, c
    and a
    jp nz, nes_controller_latch
    ret

; Output A bit0 = next NES controller bit.
nes_controller_read:
    ld a, [nes_controller_strobe]
    and a
    jr z, .shift

    call nes_controller_latch
    ld a, [nes_controller_shift]
    and $01
    ret

.shift:
    ld a, [nes_controller_shift]
    ld b, a
    and $01
    ld c, a

    ld a, b
    srl a
    or $80
    ld [nes_controller_shift], a

    ld a, c
    ret
