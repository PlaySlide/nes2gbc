; NES controller port 1 mapped to the Game Boy Color joypad.

SECTION "NES input helpers", ROM0

; Apply the current debug crop on top of the game's own NES scroll values.
nes_view_apply_scroll:
    ld a, [nes_ppu_scroll_x]
    ld b, a
    ld a, [nes_view_x]
    add b
    ldh [rSCX], a

    ld a, [nes_ppu_scroll_y]
    ld b, a
    ld a, [nes_view_y]
    add b
    ldh [rSCY], a
    ret

; Cycle TL -> TR -> BL -> BR -> center -> TL.
nes_view_cycle:
    ld a, [nes_view_mode]
    inc a
    cp $05
    jr c, .mode_ready
    xor a
.mode_ready:
    ld [nes_view_mode], a

    and a
    jr z, .top_left
    cp $01
    jr z, .top_right
    cp $02
    jr z, .bottom_left
    cp $03
    jr z, .bottom_right

    ; center: (256-160)/2, (240-144)/2 = 48,48
    ld a, $30
    ld [nes_view_x], a
    ld [nes_view_y], a
    jp nes_view_apply_scroll

.top_right:
    ld a, $60
    ld [nes_view_x], a
    xor a
    ld [nes_view_y], a
    jp nes_view_apply_scroll

.bottom_left:
    xor a
    ld [nes_view_x], a
    ld a, $60
    ld [nes_view_y], a
    jp nes_view_apply_scroll

.bottom_right:
    ld a, $60
    ld [nes_view_x], a
    ld [nes_view_y], a
    jp nes_view_apply_scroll

.top_left:
    xor a
    ld [nes_view_x], a
    ld [nes_view_y], a
    jp nes_view_apply_scroll


nes_controller_latch:
    ; Action buttons: GBC A/B/Select/Start map directly to NES bits 0-3.
    ld a, $10
    ldh [rP1], a
    ldh a, [rP1]
    cpl
    and $0F
    ld b, a

    ; GBC Select is a temporary debug-camera key. Edge-trigger the viewport
    ; cycle and suppress Select from the emulated NES controller.
    bit 2, b
    jr z, .select_released
    ld a, [nes_view_select_prev]
    and a
    jr nz, .select_held
    ld a, $01
    ld [nes_view_select_prev], a
    push bc
    call nes_view_cycle
    pop bc
.select_held:
    res 2, b
    jr .select_done
.select_released:
    xor a
    ld [nes_view_select_prev], a
.select_done:

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
