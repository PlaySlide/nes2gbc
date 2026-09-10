; Selective steady-state horizontal stitch catch-up.
; This deliberately leaves the proven generic/full-rebuild renderer untouched.
; Initial stitch activation, discontinuous area jumps, split loss, and every
; non-SMB-style path fall straight back to nes_video_update_horizontal_stitch.

SECTION "NES selective stitch", ROM0

; Drop-in VBlank entry point for the existing horizontal stitch updater.
; Only intercept an already-valid vertical-mirroring split whose coarse key
; moved by 1..8 tiles. Those are the periodic steady-scroll catch-ups seen in
; the SMB video log. Everything else uses the original implementation.
nes_video_update_horizontal_stitch_selective:
    ld a, [nes_mirroring]
    cp $01
    jp nz, nes_video_update_horizontal_stitch

    ldh a, [nes_split_active]
    and a
    jp z, nes_video_update_horizontal_stitch

    ld a, [nes_hstitch_valid]
    and a
    jp z, nes_video_update_horizontal_stitch

    ; effective X = lower NES scroll + crop offset.
    ldh a, [nes_split_bottom_x]
    ld b, a
    ldh a, [nes_view_x]
    add b
    ld c, a
    ld b, $00
    jr nc, .no_page_carry
    inc b
.no_page_carry:

    ; key = ((PPUCTRL.bit0 XOR carry) << 5) | (effective_x >> 3)
    ldh a, [nes_split_bottom_ctrl]
    and $01
    xor b
    and $01
    swap a
    add a
    ld b, a
    ld a, c
    srl a
    srl a
    srl a
    or b
    ld c, a

    ld a, [nes_hstitch_key]
    cp c
    ret z
    ld b, a                    ; B = old coarse key
    ld a, c
    ld [nes_hstitch_target_key], a

    ; Match the original updater's catch-up window exactly. Larger jumps are
    ; real transitions and must retain its LCD-off full-rebuild semantics.
    ld a, c
    sub b
    and $3F
    cp $09
    jr c, .catchup_forward

    ld a, b
    sub c
    and $3F
    cp $09
    jr c, .catchup_backward

    jp nes_video_update_horizontal_stitch

.catchup_forward:
    ld a, [nes_diag_event_flags]
    or NES_DIAG_EVENT_CATCHUP
    ld [nes_diag_event_flags], a

    ld a, [nes_hstitch_catchups]
    inc a
    ld [nes_hstitch_catchups], a
.forward_loop:
    ld a, [nes_hstitch_key]
    ld b, a                    ; column falling off the left edge
    inc a
    and $3F
    ld [nes_hstitch_key], a
    ld a, b
    and $1F
    call nes_video_refresh_stitch_column_selective

    ld a, [nes_hstitch_target_key]
    ld b, a
    ld a, [nes_hstitch_key]
    cp b
    jr nz, .forward_loop
    xor a
    ld [nes_hstitch_dirty], a
    ret

.catchup_backward:
    ld a, [nes_diag_event_flags]
    or NES_DIAG_EVENT_CATCHUP
    ld [nes_diag_event_flags], a

    ld a, [nes_hstitch_catchups]
    inc a
    ld [nes_hstitch_catchups], a
.backward_loop:
    ld a, [nes_hstitch_key]
    dec a
    and $3F
    ld [nes_hstitch_key], a
    and $1F
    call nes_video_refresh_stitch_column_selective

    ld a, [nes_hstitch_target_key]
    ld b, a
    ld a, [nes_hstitch_key]
    cp b
    jr nz, .backward_loop
    xor a
    ld [nes_hstitch_dirty], a
    ret

; Input: A = destination GBC tile column 0..31.
;
; On a one-tile coarse scroll step, the recycled destination column changes
; ownership from one physical NES nametable to the other. Before this routine
; runs, the queue flush has already kept the destination coherent with its old
; owner. Therefore we can compare the authoritative old-owner and new-owner
; cells entirely in WRAM. Rows whose tile AND palette are identical require no
; VRAM access at all. Only genuinely changed rows pay the HBlank wait and write.
;
; This is intentionally used only by steady-state catch-up. The original full
; column routine remains the source of truth for initial/full rebuilds.
nes_video_refresh_stitch_column_selective:
    and $1F
    ld [nes_hstitch_copy_start], a
    call nes_video_hstitch_source_for_column
    ld [nes_hstitch_copy_len], a      ; new owner: 0=NT0, 1=NT1

    ; Desired pattern-table bank for every cell in the lower playfield.
    ldh a, [nes_split_bottom_ctrl]
    and $10
    srl a
    ld [nes_hstitch_copy_skip], a

    ld a, $01
    ldh [rSVBK], a

    ld a, [nes_hstitch_copy_len]
    and a
    jr z, .source0
    ld h, $D4
    jr .source_ready
.source0:
    ld h, $D0
.source_ready:
    ld a, [nes_hstitch_copy_start]
    ld l, a
    ld d, $9C
    ld e, a
    ld b, $1E                    ; 30 NES tile rows

.row_loop:
    ; New-owner tile.
    ld a, [hl]
    ld c, a

    ; The old owner is always the opposite physical nametable for the one
    ; recycled column. If its tile differs, this row definitely needs publish.
    ld a, h
    xor $04
    ld h, a
    ld a, [hl]
    cp c
    jr nz, .tile_diff

    ; Tiles match. Compare the authoritative 2-bit palette as well. These
    ; helpers touch WRAM only, so unchanged rows still avoid any VRAM wait.
    ld a, h
    xor $04
    ld h, a                      ; restore new-owner source

    push bc
    push de
    push hl
    call nes_video_authoritative_tile_palette
    ld d, a                      ; D = new-owner palette
    pop hl                       ; new-owner source

    ld a, h
    xor $04
    ld h, a                      ; old-owner source
    push de                      ; preserve new palette across helper
    push hl
    call nes_video_authoritative_tile_palette
    pop hl                       ; old-owner source
    pop de                       ; D = new-owner palette
    cp d
    jr nz, .palette_diff

    ; Identical tile and palette: the stitched destination already contains
    ; exactly what the new owner needs. Restore caller state and skip the row.
    ld a, h
    xor $04
    ld h, a
    pop de
    pop bc
    jr .advance

.palette_diff:
    ld a, h
    xor $04
    ld h, a                      ; restore new-owner source
    pop de
    pop bc
    jr .publish_row

.tile_diff:
    ld a, h
    xor $04
    ld h, a                      ; restore new-owner source

.publish_row:
    ; Publish the changed tile.
    call nes_video_wait_vram
    xor a
    ldh [rVBK], a
    ld a, c
    ld [de], a

    ; Publish its palette and captured playfield pattern bank as one cell,
    ; matching the original full-column routine exactly.
    push bc
    push de
    push hl
    call nes_video_authoritative_tile_palette
    ld c, a
    ld a, [nes_hstitch_copy_skip]
    or c
    ld c, a
    pop hl
    pop de

    call nes_video_wait_vram
    ld a, $01
    ldh [rVBK], a
    ld a, c
    ld [de], a
    pop bc

.advance:
    ld a, l
    add $20
    ld l, a
    jr nc, .source_row_ok
    inc h
.source_row_ok:
    ld a, e
    add $20
    ld e, a
    jr nc, .dest_row_ok
    inc d
.dest_row_ok:
    dec b
    jr nz, .row_loop

    xor a
    ldh [rVBK], a
    ret
