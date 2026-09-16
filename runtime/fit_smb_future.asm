; FIT-screen SMB future-backing maintenance.
;
; The 160px viewport scans scaled offsets 0..20 from a 32-column physical ring.
; SMB constructs future columns incrementally in authoritative NES nametable
; WRAM. Visible writes can publish immediately, but offsets 21..31 must not be
; recomposed per source byte: doing that is too expensive. Instead mark the
; affected scaled world column dirty and refresh it once, in small VBlank chunks,
; before it reaches scanout.

SECTION "FIT SMB future backing state", WRAM0[$CA00]
nes_fit_future_dirty:      ds 5 ; 40 scaled world columns, one bit each
nes_fit_future_active_col: ds 1 ; world column 0..39, $FF = none
nes_fit_future_active_row: ds 1 ; next FIT row, 2..14

SECTION "FIT SMB future backing", ROM0

nes_gbc_fit_smb_future_init:
    xor a
    ld hl, nes_fit_future_dirty
    ld b, $05
.clear_dirty:
    ld [hli], a
    dec b
    jr nz, .clear_dirty
    ld a, $FF
    ld [nes_fit_future_active_col], a
    ld a, $02
    ld [nes_fit_future_active_row], a
    ret

; HL = authoritative physical NES tile address. Preserve the existing immediate
; publication for offsets 0..20, then coalesce any touched future scaled output
; columns (21..31) into a 40-bit world-column dirty set.
nes_gbc_fit_smb_publish_backing_hl:
    push hl
    call nes_gbc_fit_smb_publish_visible_hl
    pop hl

    ; Source world tile X. SMB split mode is vertical mirroring, so physical
    ; nametable 1 is horizontal world columns 32..63.
    ld a, l
    and $1F
    ld c, a
    ld a, h
    and $04
    jr z, .src_x_ready
    ld a, c
    or $20
    ld c, a
.src_x_ready:

    ; host_x = source_tile_x * 5. One NES source tile overlaps at most two
    ; scaled 8px output columns. D = first world column, E = remainder.
    ld d, $00
    ld e, c
    ld h, d
    ld l, e
    add hl, hl
    add hl, hl
    add hl, de
    ld a, l
    and $07
    ld e, a
    srl h
    rr l
    srl h
    rr l
    srl h
    rr l
    ld a, l
    cp 40
    jr c, .dest0_ready
    sub 40
.dest0_ready:
    ld d, a

    ld a, d
    call .mark_if_future

    ld a, e
    cp 4
    ret c
    ld a, d
    inc a
    cp 40
    jr c, .dest1_ready
    sub 40
.dest1_ready:
    jp .mark_if_future

; A = scaled world column 0..39. Mark only if it is currently in the resident
; future backing range 21..31. Outside the 32-column ring needs no work.
.mark_if_future:
    ld b, a
    ld a, [nes_fit_origin_mx]
    ld c, a
    ld a, b
    sub c
    jr nc, .delta_ready
    add 40
.delta_ready:
    cp 21
    ret c
    cp 32
    ret nc
    ld a, b
    jp nes_gbc_fit_smb_mark_future_a

; A = scaled world column 0..39. Set its dirty bit.
nes_gbc_fit_smb_mark_future_a:
    push bc
    push hl
    ld c, a
    and $07
    ld b, $01
    jr z, .mask_ready
.mask_loop:
    sla b
    dec a
    jr nz, .mask_loop
.mask_ready:
    ld a, c
    srl a
    srl a
    srl a
    ld l, a
    ld h, HIGH(nes_fit_future_dirty)
    ld a, [hl]
    or b
    ld [hl], a
    pop hl
    pop bc
    ret

; A = scaled world column 0..39. Atomically consume its dirty bit.
; Returns A=1 when a bit was present, A=0 otherwise.
nes_gbc_fit_smb_take_future_a:
    push bc
    push hl
    ld c, a
    and $07
    ld b, $01
    jr z, .mask_ready
.mask_loop:
    sla b
    dec a
    jr nz, .mask_loop
.mask_ready:
    ld a, c
    srl a
    srl a
    srl a
    ld l, a
    ld h, HIGH(nes_fit_future_dirty)
    ld a, [hl]
    and b
    jr z, .none
    ld a, b
    cpl
    ld c, a
    ld a, [hl]
    and c
    ld [hl], a
    ld a, $01
    jr .done
.none:
    xor a
.done:
    pop hl
    pop bc
    ret

; Refresh at most four rows of one dirty future world column per host VBlank.
; New parser writes that arrive while a column is in progress set its dirty bit
; again, causing one later authoritative pass instead of being lost.
nes_gbc_fit_smb_service_future:
    ld a, [nes_fit_screen]
    and a
    ret z
    ld a, [nes_mirroring]
    cp $01
    ret nz
    ldh a, [nes_split_active]
    and a
    ret z

    ; Do not compete with a recycled-column/full-page pass already using the
    ; compositor this VBlank.
    ld a, [nes_fit_dirty]
    and a
    ret nz

    call nes_video_fit_vblank_ok
    ret z

    ld a, [nes_fit_future_active_col]
    cp $FF
    jr nz, .have_active

    ; Prefer the resident dirty column nearest the left edge. Normally all bits
    ; are still future (21..31); scanning 0..20 too guarantees that a delayed
    ; refresh can never become a permanent visible hole if work briefly backs up.
    ld b, 0
.scan:
    ld a, [nes_fit_origin_mx]
    add b
    cp 40
    jr c, .world_ready
    sub 40
.world_ready:
    ld c, a
    push bc
    ld a, c
    call nes_gbc_fit_smb_take_future_a
    pop bc
    and a
    jr nz, .start_active
    inc b
    ld a, b
    cp 32
    jr c, .scan
    ret

.start_active:
    ld a, c
    ld [nes_fit_future_active_col], a
    ld a, $02
    ld [nes_fit_future_active_row], a

.have_active:
    ; Convert the persistent world column back to its current ring offset. This
    ; keeps ownership correct even if the camera advances while the 4-row pass
    ; is in progress.
    ld a, [nes_fit_future_active_col]
    ld b, a
    ld a, [nes_fit_origin_mx]
    ld c, a
    ld a, b
    sub c
    jr nc, .active_delta_ready
    add 40
.active_delta_ready:
    cp 32
    jr c, .active_resident

    ; It left the resident ring before completion. No stale physical ownership
    ; remains to repair; any later construction will mark it again when resident.
    ld a, $FF
    ld [nes_fit_future_active_col], a
    ld a, $02
    ld [nes_fit_future_active_row], a
    ret

.active_resident:
    ld [nes_fit_mt_mx], a
    ld a, [nes_fit_future_active_row]
    cp 15
    jr nc, .finish
    ld [nes_fit_mt_my], a

    ld b, $04
.row_loop:
    call nes_video_fit_vblank_ok
    ret z
    push bc
    call nes_video_fit_publish_at_mx_my
    pop bc

    ld a, [nes_fit_future_active_row]
    inc a
    ld [nes_fit_future_active_row], a
    cp 15
    jr nc, .finish
    ld [nes_fit_mt_my], a

    dec b
    jr nz, .row_loop
    ret

.finish:
    ld a, $FF
    ld [nes_fit_future_active_col], a
    ld a, $02
    ld [nes_fit_future_active_row], a
    ret
