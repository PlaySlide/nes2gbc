; NES controller port 1 mapped to the Game Boy Color joypad.

SECTION "NES input helpers", ROM0

; Apply the current debug crop on top of the game's own NES scroll values.
nes_view_apply_scroll:
    ld a, [nes_ppu_scroll_x]
    ld b, a
    ldh a, [nes_view_x]
    add b
    ldh [rSCX], a

    ; The GBC crop is an offset inside the 256x240 NES viewport, not inside a
    ; 256x256 CGB map. If NES scroll Y + crop Y reaches 240, the crop begins in
    ; the vertically adjacent logical NES nametable. Select that table and
    ; subtract 240 instead of wrapping through CGB rows 30-31 back into the
    ; top of the same map.
    ld a, [nes_ppu_scroll_y]
    ld b, a
    ldh a, [nes_view_y]
    add b
    ld d, a
    jr c, .vertical_wrap
    cp $F0
    jr nc, .vertical_wrap

    ld a, [nes_ppuctrl]
    call nes_video_apply_map_select_a
    ld a, d
    ldh [rSCY], a
    ret

.vertical_wrap:
    ld a, [nes_ppuctrl]
    xor $02
    call nes_video_apply_map_select_a
    ld a, d
    add $10
    ldh [rSCY], a
    ret

; Mark viewport hardware state dirty without changing it mid-scanline.
nes_view_mark_dirty:
    ld a, $01
    ldh [nes_scroll_dirty], a
    ret

; First-pass generic follow camera. Acquire the sprite nearest the NES screen
; center, then track the nearest sprite to the previous anchor. A dead-zone
; keeps composite-sprite animation from making the crop wobble every frame.
DEF FOLLOW_TRACK_RADIUS EQU 16
DEF FOLLOW_LOST_LIMIT EQU 60

nes_view_follow_update:
    ld a, [nes_view_follow_enabled]
    and a
    ret z

    ; Once acquired, track the sprite by position, not by OAM slot: many
    ; games rotate OAM slot order every frame for flicker multiplexing, so a
    ; fixed slot hops between unrelated objects. Each frame pick the visible
    ; sprite nearest the previous anchor; if none is within
    ; FOLLOW_TRACK_RADIUS, hold the camera (target hidden or flickered out).
    ld a, [nes_view_follow_valid]
    and a
    jr z, .acquire
    ld a, [nes_view_follow_x]
    ld [nes_view_follow_ref_x], a
    ld a, [nes_view_follow_y]
    ld [nes_view_follow_ref_y], a
    jr .scan_from_ref

.acquire:
    ld a, $80
    ld [nes_view_follow_ref_x], a
    ld a, $78
    ld [nes_view_follow_ref_y], a

.scan_from_ref:
    ld a, $FF
    ld [nes_view_follow_best_dist], a
    ld hl, nes_oam_ram
    ld b, 64

.scan:
    ; NES OAM Y is top-minus-one; $EF+ is conventionally hidden/offscreen.
    ld a, [hli]
    cp $EF
    jp nc, .skip_three
    inc a
    ld [nes_view_follow_candidate_y], a

    ; Skip tile + attributes, then read X.
    inc hl
    inc hl
    ld a, [hli]
    ld [nes_view_follow_candidate_x], a

    ; Chebyshev distance = max(abs(dx), abs(dy)); no 8-bit sum overflow.
    ld c, a
    ld a, [nes_view_follow_ref_x]
    sub c
    jr nc, .dx_ready
    cpl
    inc a
.dx_ready:
    ld d, a

    ld a, [nes_view_follow_candidate_y]
    ld c, a
    ld a, [nes_view_follow_ref_y]
    sub c
    jr nc, .dy_ready
    cpl
    inc a
.dy_ready:
    cp d
    jr nc, .distance_ready
    ld a, d
.distance_ready:
    ld c, a

    ld a, [nes_view_follow_best_dist]
    cp c
    jr c, .next
    jr z, .next

    ld a, c
    ld [nes_view_follow_best_dist], a

    ; Current slot index = 64 - remaining-count B.
    ld a, $40
    sub b
    ld [nes_view_follow_slot], a

.next:
    dec b
    jp nz, .scan
    jr .finish

.skip_three:
    inc hl
    inc hl
    inc hl
    dec b
    jp nz, .scan

.finish:
    ld a, [nes_view_follow_valid]
    and a
    ld a, [nes_view_follow_best_dist]
    jr z, .finish_acquire
    cp FOLLOW_TRACK_RADIUS + 1
    jr c, .accept
    ; Target not found near the anchor: hold the camera. After
    ; FOLLOW_LOST_LIMIT consecutive misses (teleport, room change) reacquire.
    ld a, [nes_view_follow_was_valid]
    inc a
    ld [nes_view_follow_was_valid], a
    cp FOLLOW_LOST_LIMIT
    ret c
    xor a
    ld [nes_view_follow_valid], a
    ld [nes_view_follow_was_valid], a
    ret
.finish_acquire:
    cp $FF
    ret z
.accept:
    xor a
    ld [nes_view_follow_was_valid], a
    ; Re-read the winning slot (recorded by the scan) as the new anchor.
    ld a, [nes_view_follow_slot]
    add a
    add a
    ld l, a
    ld h, HIGH(nes_oam_ram)
    ld a, [hli]
    inc a
    ld [nes_view_follow_y], a
    inc hl
    inc hl
    ld a, [hl]
    ld [nes_view_follow_x], a
    ld a, $01
    ld [nes_view_follow_valid], a

.camera:
    ; Horizontal dead-zone: keep target within viewport-relative X 56..104.
    ldh a, [nes_view_x]
    ld c, a
    add $38
    ld d, a
    ld a, [nes_view_follow_x]
    cp d
    jr nc, .check_right

    cp $38
    jr nc, .move_left
    xor a
    jr .store_x
.move_left:
    sub $38
.store_x:
    ldh [nes_view_x], a
    jr .camera_y

.check_right:
    ld a, c
    add $68
    ld d, a
    ld a, [nes_view_follow_x]
    cp d
    jr c, .camera_y
    jr z, .camera_y
    sub $68
    cp $61
    jr c, .store_right
    ld a, $60
.store_right:
    ldh [nes_view_x], a

.camera_y:
    ; Vertical dead-zone: viewport-relative Y 48..96.
    ldh a, [nes_view_y]
    ld c, a
    add $30
    ld d, a
    ld a, [nes_view_follow_y]
    cp d
    jr nc, .check_bottom

    cp $30
    jr nc, .move_up
    xor a
    jr .store_y
.move_up:
    sub $30
.store_y:
    ldh [nes_view_y], a
    jp nes_view_mark_dirty

.check_bottom:
    ld a, c
    add $60
    ld d, a
    ld a, [nes_view_follow_y]
    cp d
    jr c, .done_camera
    jr z, .done_camera
    sub $60
    cp $61
    jr c, .store_bottom
    ld a, $60
.store_bottom:
    ldh [nes_view_y], a
.done_camera:
    jp nes_view_mark_dirty

; Select+A toggles Follow <-> Manual. Manual starts centered.
nes_view_toggle_follow:
    ld a, [nes_view_follow_enabled]
    xor $01
    ld [nes_view_follow_enabled], a
    and a
    jr z, .manual

    xor a
    ld [nes_view_follow_valid], a
    jp nes_view_mark_dirty

.manual:
    xor a
    ld [nes_view_follow_valid], a
    ld a, $04
    ld [nes_view_mode], a
    ld a, $30
    ldh [nes_view_x], a
    ldh [nes_view_y], a
    jp nes_view_mark_dirty

; In Follow mode, Select+B advances to the next visible sprite to the right
; of the current anchor (wrapping to the leftmost). Ordering by screen X, not
; OAM slot, keeps the cycle meaningful when games rotate OAM slots per frame.
nes_view_cycle_follow_target:
    ld a, [nes_view_follow_enabled]
    and a
    ret z

    ; C = threshold X (anchor + 8, saturating; invalid anchor -> 0).
    ld c, 0
    ld a, [nes_view_follow_valid]
    and a
    jr z, .threshold_ready
    ld a, [nes_view_follow_x]
    add 8
    jr nc, .threshold_set
    ld a, $FF
.threshold_set:
    ld c, a
.threshold_ready:
    ld d, $FF               ; best X > threshold
    ld e, $FF               ; its slot
    ld a, $FF
    ld [nes_view_follow_ref_x], a   ; leftmost X overall
    ld [nes_view_follow_ref_y], a   ; its slot
    ld hl, nes_oam_ram
    ld b, 0                 ; slot
.cycle_scan:
    ld a, [hl]
    cp $EF
    jr nc, .cycle_next
    inc l
    inc l
    inc l
    ld a, [hl]              ; X
    dec l
    dec l
    dec l
    ld [nes_view_follow_candidate_x], a
    push hl
    ld hl, nes_view_follow_ref_x
    cp [hl]
    jr nc, .not_leftmost
    ld [hl], a
    ld hl, nes_view_follow_ref_y
    ld [hl], b
.not_leftmost:
    pop hl
    ld a, [nes_view_follow_candidate_x]
    ; strictly right of threshold and left of best
    cp c
    jr c, .cycle_next
    jr z, .cycle_next
    cp d
    jr nc, .cycle_next
    ld d, a
    ld e, b
.cycle_next:
    inc l
    inc l
    inc l
    inc l
    inc b
    ld a, b
    cp 64
    jr nz, .cycle_scan

    ld a, e
    cp $FF
    jr nz, .cycle_found
    ld a, [nes_view_follow_ref_y]
    cp $FF
    ret z
.cycle_found:
    ld [nes_view_follow_slot], a
    add a
    add a
    ld l, a
    ld h, HIGH(nes_oam_ram)
    ld a, [hli]
    inc a
    ld [nes_view_follow_y], a
    inc hl
    inc hl
    ld a, [hl]
    ld [nes_view_follow_x], a
    xor a
    ld [nes_view_follow_was_valid], a
    ld a, $01
    ld [nes_view_follow_valid], a
    jp nes_view_follow_update

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
    ldh [nes_view_x], a
    ldh [nes_view_y], a
    jp nes_view_mark_dirty

.top_right:
    ld a, $60
    ldh [nes_view_x], a
    xor a
    ldh [nes_view_y], a
    jp nes_view_mark_dirty

.bottom_left:
    xor a
    ldh [nes_view_x], a
    ld a, $60
    ldh [nes_view_y], a
    jp nes_view_mark_dirty

.bottom_right:
    ld a, $60
    ldh [nes_view_x], a
    ldh [nes_view_y], a
    jp nes_view_mark_dirty

.top_left:
    xor a
    ldh [nes_view_x], a
    ldh [nes_view_y], a
    jp nes_view_mark_dirty


nes_controller_latch:
    ; Action buttons: GBC A/B/Select/Start map directly to NES bits 0-3.
    ld a, $10
    ldh [rP1], a
    ldh a, [rP1]
    cpl
    and $0F
    ld b, a

    ; Camera chords are edge-triggered and consumed so the NES game never
    ; sees them. Select+A toggles Follow/Manual. Select+B cycles visible follow
    ; targets. In Manual, Select+Start cycles TL -> TR -> BL -> BR -> Center.
    bit 2, b
    jr z, .view_chord_released

    bit 0, b
    jr z, .check_follow_cycle_chord
    ld a, [nes_view_select_prev]
    and a
    jr nz, .follow_chord_held
    ld a, $01
    ld [nes_view_select_prev], a
    push bc
    call nes_view_toggle_follow
    pop bc
.follow_chord_held:
    res 0, b
    res 2, b
    jr .view_chord_done

.check_follow_cycle_chord:
    bit 1, b
    jr z, .check_cycle_chord
    ld a, [nes_view_select_prev]
    and a
    jr nz, .follow_cycle_chord_held
    ld a, $01
    ld [nes_view_select_prev], a
    push bc
    call nes_view_cycle_follow_target
    pop bc
.follow_cycle_chord_held:
    res 1, b
    res 2, b
    jr .view_chord_done

.check_cycle_chord:
    bit 3, b
    jr z, .view_chord_released
    ld a, [nes_view_select_prev]
    and a
    jr nz, .cycle_chord_held
    ld a, $01
    ld [nes_view_select_prev], a

    ld a, [nes_view_follow_enabled]
    and a
    jr nz, .cycle_chord_held
    push bc
    call nes_view_cycle
    pop bc
.cycle_chord_held:
    res 2, b
    res 3, b
    jr .view_chord_done

.view_chord_released:
    xor a
    ld [nes_view_select_prev], a
.view_chord_done:

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
