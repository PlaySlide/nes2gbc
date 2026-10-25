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

; Follow camera: the logic lives in the runtime helper bank (BANK 255) to
; keep ROM0 within budget (APU=1 builds). These ROM0 entry points switch to
; the helper bank and restore the translated-code bank on return.
nes_view_follow_update:
    ld a, [nes_view_follow_enabled]
    and a
    ret z
    ld a, $FF
    ld [$2000], a
    xor a
    ; MBC5 bit 8 stays 0 (cleared at boot; all code banks < 256)
    call nes_view_follow_update_romx
    jp nes_restore_code_bank

nes_view_toggle_follow:
    ld a, $FF
    ld [$2000], a
    xor a
    ; MBC5 bit 8 stays 0 (cleared at boot; all code banks < 256)
    call nes_view_toggle_follow_romx
    jp nes_restore_code_bank

nes_view_cycle_follow_target:
    ld a, $FF
    ld [$2000], a
    xor a
    ; MBC5 bit 8 stays 0 (cleared at boot; all code banks < 256)
    call nes_view_cycle_follow_target_romx
    jp nes_restore_code_bank

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


SECTION "NES follow camera helper", ROMX, BANK[255]

; First-pass generic follow camera. Acquire the sprite nearest the NES screen
; center, then track the nearest sprite to the previous anchor. A dead-zone
; keeps composite-sprite animation from making the crop wobble every frame.
DEF FOLLOW_TRACK_RADIUS EQU 16
DEF FOLLOW_LOST_LIMIT EQU 60

nes_view_follow_update_romx:
    ld a, [nes_view_follow_enabled]
    and a
    ret z

    ld a, [nes_view_follow_hint_mode]
    and a
    jp nz, nes_view_follow_hinted

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
    ; Registers: D = ref X, E = ref Y, B = best distance, C = |dy|.
    ld a, [nes_view_follow_ref_x]
    ld d, a
    ld a, [nes_view_follow_ref_y]
    ld e, a
    ld b, $FF
    ; Unrolled over the 64 fixed canonical OAM entries (absolute loads).
FOR I, 64
    ; NES OAM Y is top-minus-one; $EF+ is conventionally hidden/offscreen.
    ld a, [nes_oam_ram + 4 * I]
    cp $EF
    jr nc, .s_n{d:I}
    inc a
    ; Chebyshev distance = max(abs(dx), abs(dy)); no 8-bit sum overflow.
    sub e
    jr nc, .s_p{d:I}
    cpl
    inc a
.s_p{d:I}:
    ld c, a
    ld a, [nes_oam_ram + 4 * I + 3]
    sub d
    jr nc, .s_q{d:I}
    cpl
    inc a
.s_q{d:I}:
    cp c
    jr nc, .s_r{d:I}
    ld a, c
.s_r{d:I}:
    ; Strictly nearer than the best so far: record it and its slot.
    cp b
    jr nc, .s_n{d:I}
    ld b, a
    ld a, I
    ld [nes_view_follow_slot], a
.s_n{d:I}:
ENDR

.finish:
    ld a, b
    ld [nes_view_follow_best_dist], a
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
nes_view_follow_camera::
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
    ; PocketNES hints drive Y proportionally (nes_view_follow_hinted).
    ld a, [nes_view_follow_hint_mode]
    and a
    jp nz, nes_view_mark_dirty
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

; PocketNES-hinted follow (data-driven from the PocketNES menu-maker DB).
; Mode 1 follows NES OAM sprite N; mode 2 follows the screen-Y byte at a NES
; RAM address, which is immune to OAM slot rotation. Like PocketNES
; (ppu.s: windowtop = Y*5/16), Y > 239 holds the camera and the crop top is
; proportional to Y: view_y = Y*2/5 (0..96 for Y 0..239), so the player sits
; at 0.6*Y on the 144-line screen. PocketNES does not pan horizontally (the
; GBA is 240 wide); the 160-wide GBC crop pans X with the usual dead-zone,
; anchored on the hinted sprite (mode 1) or on the visible sprite nearest the
; previous anchor X among those within 24 lines of the RAM Y (mode 2).
nes_view_follow_hinted:
    cp $01
    jr nz, .mem
    ld a, [nes_view_follow_slot]
    add a
    add a
    ld l, a
    ld h, HIGH(nes_oam_ram)
    ld a, [hli]
    cp $F0
    ret nc
    ld [nes_view_follow_y], a
    inc hl
    inc hl
    ld a, [hl]
    ld [nes_view_follow_x], a
    ; Sprite mode: the hinted slot is the player, so the X dead-zone tracks it.
    call .proportional_y
    jp nes_view_follow_camera

.mem:
    ld a, [nes_view_follow_hint_lo]
    ld l, a
    ld a, [nes_view_follow_hint_hi]
    ld h, a
    ld a, [hl]
    cp $F0
    ret nc
    ld [nes_view_follow_y], a
    ; X: nearest-to-anchor sprite within the RAM Y band.
    ld e, a                 ; E = target Y
    ld a, [nes_view_follow_valid]
    and a
    ld a, $80
    jr z, .anchor_ready
    ld a, [nes_view_follow_x]
.anchor_ready:
    ld c, a                 ; C = anchor X
    ld d, $FF               ; D = best |dx|
    ; B = best X. Unrolled over the 64 fixed canonical OAM entries.
FOR I, 64
    ld a, [nes_oam_ram + 4 * I]
    cp $EF
    jr nc, .b_n{d:I}
    sub e
    jr nc, .b_p{d:I}
    cpl
    inc a
.b_p{d:I}:
    cp 32
    jr nc, .b_n{d:I}
    ld a, [nes_oam_ram + 4 * I + 3]
    sub c
    jr nc, .b_q{d:I}
    cpl
    inc a
.b_q{d:I}:
    cp d
    jr nc, .b_n{d:I}
    ld d, a
    ld a, [nes_oam_ram + 4 * I + 3]
    ld b, a
.b_n{d:I}:
ENDR
    ld a, d
    cp $FF
    jr z, .apply_y_only
    ld a, b
    ld [nes_view_follow_best_dist], a   ; best X
    ; With a valid anchor, accept only a sprite within 16px of it (bullets and
    ; enemies share the player's lines); else hold X, and reacquire nearest
    ; screen centre after FOLLOW_LOST_LIMIT misses.
    ld a, [nes_view_follow_valid]
    and a
    jr z, .band_accept
    ld a, d
    cp FOLLOW_TRACK_RADIUS + 1
    jr c, .band_accept
    ld a, [nes_view_follow_was_valid]
    inc a
    ld [nes_view_follow_was_valid], a
    cp FOLLOW_LOST_LIMIT
    jr c, .apply_y_only
    xor a
    ld [nes_view_follow_valid], a
    ld [nes_view_follow_was_valid], a
    jr .apply_y_only
.band_accept:
    xor a
    ld [nes_view_follow_was_valid], a
    ld a, [nes_view_follow_best_dist]
    ld [nes_view_follow_x], a
    ld a, $01
    ld [nes_view_follow_valid], a
.apply:
    ; X is proportional too: view_x = X/4 + X/8 (0..95), so the anchor sits
    ; at 5/8*X on the 160-wide screen and a wrong or swapped anchor moves the
    ; crop by only 3/8 of the difference (the player stays in view).
    ld a, [nes_view_follow_x]
    srl a
    srl a
    ld b, a
    srl a
    add b
    ldh [nes_view_x], a
    call .proportional_y
    jp nes_view_mark_dirty
.apply_y_only:
    call .proportional_y
    jp nes_view_mark_dirty

.proportional_y:
    ; view_y = Y/2 - Y/8 + Y/32 (~0.406*Y), clamped to 96.
    ld a, [nes_view_follow_y]
    ld b, a
    srl a
    ld c, a                 ; Y/2
    ld a, b
    srl a
    srl a
    srl a
    ld d, a                 ; Y/8
    ld a, b
    swap a
    and $0F
    srl a                   ; Y/32
    add c
    sub d
    cp $61
    jr c, .py_store
    ld a, $60
.py_store:
    ldh [nes_view_y], a
    ret

; Select+A toggles Follow <-> Manual. Manual starts centered.
nes_view_toggle_follow_romx:
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
nes_view_cycle_follow_target_romx:
    ld a, [nes_view_follow_enabled]
    and a
    ret z
    ; A manual target choice overrides any PocketNES hint.
    xor a
    ld [nes_view_follow_hint_mode], a

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
    jp nes_view_follow_update_romx


; Body of nes_unimplemented (see cpu.asm); runs with bank $FF mapped and never returns.
nes_unimplemented_romx:
    ; Snapshot the computed-control-flow state before stopping. This has zero
    ; cost in normal execution and is especially useful for SMB's JumpEngine,
    ; which leaves its caller pointer in $04/$05 and computed target in $06/$07.
    ld a, [nes_dispatch_cache_pc_lo]
    ldh [nes_fault_target_lo], a
    ld a, [nes_dispatch_cache_pc_hi]
    ldh [nes_fault_target_hi], a
    ldh a, [nes_sp]
    ldh [nes_fault_sp_snapshot], a
    ldh a, [nes_a]
    ldh [nes_fault_a_snapshot], a
    ldh a, [nes_x]
    ldh [nes_fault_x_snapshot], a
    ldh a, [nes_y]
    ldh [nes_fault_y_snapshot], a

    ld a, [$C004]
    ldh [nes_fault_zp04_snapshot], a
    ld a, [$C005]
    ldh [nes_fault_zp05_snapshot], a
    ld a, [$C006]
    ldh [nes_fault_zp06_snapshot], a
    ld a, [$C007]
    ld [nes_fault_zp07_snapshot], a

    ld a, $01
    ldh [nes_fault_kind], a
    ld a, $FF
    ldh [nes_fault_hram], a
    ld [nes_debug_fault], a
    di
.hang:
    halt
    jr .hang

IF DEF(NES2GBC_ISR_BANK_FIX)
MACRO BANK_IN_A ; first PC, one-past-last PC: the bank is still in A
    ld a, e
    sub LOW(\1)
    ld a, d
    sbc HIGH(\1)
    jr c, .na\@
    ld a, e
    sub LOW(\2)
    ld a, d
    sbc HIGH(\2)
    jr nc, .na\@
    ld hl, sp + 13          ; interrupted code's A
    ld a, [hl]
    jp .set
.na\@:
ENDM
MACRO BANK_IN_B ; first PC, one-past-last PC
    ld a, e
    sub LOW(\1)
    ld a, d
    sbc HIGH(\1)
    jr c, .nx\@
    ld a, e
    sub LOW(\2)
    ld a, d
    sbc HIGH(\2)
    jr nc, .nx\@
    ld hl, sp + 11          ; interrupted code's B
    ld a, [hl]
    jp .set
.nx\@:
ENDM
; Called by nes_isr_fix_bank (ROM0) from the end of the VBlank ISR with bank
; $FF mapped; returns the bank in A. Stack: [ret][ret][F,A (rSVBK)][L,H][E,D][C,B][F,A user][PC lo,hi]
; then the interrupted code's own stack. Chooses the ROM bank that was mapped
; when the interrupt hit (the ISR helpers leave nes_current_code_bank mapped)
; and returns it in A. Cases, in order:
;  1. inside the dispatch-table lookup window: table bank of the target PC
;  2. in ROM0 right after `ld [$2000],a` (before `ld l,[hl]`/`xor a`): A
;  3. in ROM0 on `call`/`xor a`+`call` of a bank-stub: the stub's immediate
;  4. inside a helper-bank routine (any bank-stub `ld a,N; ld [$2000],a;
;     [xor a;] call X` return address live on the interrupted stack): N
;  5. otherwise the translated-code bank.
nes_isr_fix_bank_romx:
    ld hl, sp + 14
    ld a, [hli]
    ld e, a
    ld d, [hl]              ; DE = interrupted PC
    inc hl
    ld b, h
    ld c, l                 ; BC = first live word of the interrupted stack
    ld a, d
    cp $40
    jp nc, .walk
    ; (1) dispatch-table lookup window
    ld a, e
    sub LOW(nes_dispatch_hl.tbl_win_start)
    ld a, d
    sbc HIGH(nes_dispatch_hl.tbl_win_start)
    jp c, .rom0
    ld a, e
    sub LOW(nes_dispatch_hl.tbl_win_end)
    ld a, d
    sbc HIGH(nes_dispatch_hl.tbl_win_end)
    jp nc, .rom0
    ld a, [nes_dispatch_cache_pc_hi]
    swap a
    and $0F
    add $18
    jp .set
.rom0:
IF DEF(NES2GBC_RAM_OVERLAY)
    ; (1a) PRG-RAM overlay dispatch after it selected the overlay table bank
    ld a, e
    sub LOW(nes_overlay_dispatch_hl.win_start)
    ld a, d
    sbc HIGH(nes_overlay_dispatch_hl.win_start)
    jp c, .ovl_no
    ld a, e
    sub LOW(nes_overlay_dispatch_hl.win_end)
    ld a, d
    sbc HIGH(nes_overlay_dispatch_hl.win_end)
    jp nc, .ovl_no
    ld hl, sp + 11          ; interrupted code's B (= the dispatched PC's high byte)
    ld a, [hl]
    bit 4, a
    ld a, BANK(nes_overlay_table_6)
    jp z, .set
    ld a, BANK(nes_overlay_table_7)
    jp .set
.ovl_no:
ENDC
    ; (1b) windows that keep the mapped bank in B across the data read
    BANK_IN_B nes_mapper2_conflict_read_hl.win_start, nes_mapper2_conflict_read_hl.win_end
    BANK_IN_A nes_cpu_read.hb_win_start, nes_cpu_read.hb_win_end
    BANK_IN_A nes_cpu_read.sel_win_start, nes_cpu_read.sel_win_end
    BANK_IN_B nes_ppu_read_raw.chr_win_start, nes_ppu_read_raw.chr_win_end
    ld h, d
    ld l, e
    ld a, [hl]
    cp $CD
    jp z, .rom0_call
    cp $6E
    jp z, .a_intact
    cp $47
    jp z, .a_intact
    cp $AF
    jp nz, .walk
.a_intact:
    ; `ld [$2000],a` just executed: A is still the bank.
    dec hl
    ld a, [hl]
    cp $20
    jp nz, .walk
    dec hl
    ld a, [hl]
    and a
    jp nz, .walk
    dec hl
    ld a, [hl]
    cp $EA
    jp nz, .walk
    ld hl, sp + 13          ; interrupted code's A
    ld a, [hl]
    jp .set
.rom0_call:
    dec hl
    ld a, [hl]
    cp $AF
    jp z, .stub             ; xor a; call X
    inc hl                  ; call X directly after the write
    jp .stub
.walk:
    ld e, 0                 ; E = 1 once a non-return word was skipped
.walk_loop:
    ; Scan the interrupted stack (BC .. $D000) for a bank-stub return address.
    ld a, b
    cp $D0
    jp nc, .cur
    ld a, [bc]
    ld l, a
    inc bc
    ld a, [bc]
    ld h, a
    inc bc
    cp $40
    jp c, .walk_rom0
    cp $80
    jp nc, .walk_skip       ; not a return address
    ld d, a                 ; a ROMX return address: the interrupted code was
    jp .walk_loop           ; called from a ROMX routine (D >= $40 from here on)
.walk_skip:
    ld e, 1
    jp .walk_loop
.walk_rom0:
    dec hl
    dec hl
    dec hl
    ld a, [hl]
    cp $CD
    jp nz, .walk_skip
    ; A bank-stub frame only describes the interrupted code if a routine in
    ; the stub's bank is live: some ROMX return address (or the interrupted
    ; PC itself in ROMX) must lie between SP and the frame. Otherwise the
    ; word is saved data that merely looks like a return address.
    ld a, d
    cp $40
    jp nc, .stub_ok
    ; (A helper that tail-jumps into a ROM0 routine leaves no ROMX return
    ; address: accept the stub frame if every word above it was a verified
    ; ROM0 return address, i.e. nothing was skipped as saved data.)
    ld a, e
    and a
    jp nz, .walk_skip
.stub_ok:
    dec hl
    ld a, [hl]
    cp $AF
    jp z, .stub_walk
    inc hl
.stub_walk:
.stub:
    ; HL = address just after `ld [$2000],a`. Expect 3E nn EA 00 20 before it.
    dec hl
    ld a, [hl]
    cp $20
    jp nz, .walk_or_cur
    dec hl
    ld a, [hl]
    and a
    jp nz, .walk_or_cur
    dec hl
    ld a, [hl]
    cp $EA
    jp nz, .walk_or_cur
    dec hl
    ld e, [hl]              ; candidate bank
    dec hl
    ld a, [hl]
    cp $3E
    jp nz, .walk_or_cur
    ld a, e
    jp .set
.walk_or_cur:
    jp .walk_loop
.cur:
    ldh a, [nes_current_code_bank]
.set:
    ret                     ; the caller writes A to the MBC from ROM0
ENDC
