; FIT-screen SMB future-backing maintenance.
;
; The 160px viewport scans scaled offsets 0..20 from a 32-column physical ring.
; SMB constructs offsets 21..31 incrementally before they reach scanout.  Those
; writes must update the backing ring in the SAME completed-NMI transaction;
; deferring whole-column refreshes lets stale data survive until it is visible.
;
; A NES source tile can overlap two scaled output cells, and two source rows map
; to one FIT row.  Re-composing per source write therefore repeats expensive
; 5/8 work.  Keep the existing immediate visible publisher, but coalesce future
; work by (scaled world column, FIT row) and publish each touched future cell
; exactly once after the completed NMI queue has been consumed.

; Bank 6 already owns D000-D8FF for the published shadow/stage bitmap. D900+
; is free. Forty world columns * two bytes gives a 15-bit FIT-row dirty mask for
; each scaled world column.
SECTION "FIT SMB future backing state", WRAMX[$D900], BANK[6]
nes_fit_future_rows: ds $50

SECTION "FIT SMB future backing", ROM0

nes_gbc_fit_smb_future_init:
    ld a, $06
    ldh [rSVBK], a
    xor a
    ld hl, nes_fit_future_rows
    ld b, $50
.clear:
    ld [hli], a
    dec b
    jr nz, .clear
    ld a, $01
    ldh [rSVBK], a
    ret

; HL = authoritative physical NES tile address. Preserve f132's immediate
; publication for offsets 0..20.  If either scaled destination lies in future
; backing (21..31), mark only the affected FIT row for synchronous publication
; at the end of this completed-NMI transaction.
nes_gbc_fit_smb_publish_backing_hl:
    push hl
    call nes_gbc_fit_smb_publish_visible_hl
    pop hl

    ; Source NES tile row -> FIT row. Rows 0-1 are the fixed HUD surface and
    ; are intentionally never written by future playfield backing.
    ld a, h
    and $03
    add a
    add a
    add a
    ld b, a
    ld a, l
    and $E0
    rrca
    rrca
    rrca
    rrca
    rrca
    and $07
    add b
    srl a
    cp 15
    ret nc
    ld [nes_fit_mt_my], a
    cp 2
    ret c

    ; Vertical mirroring: physical page 1 is source world columns 32..63.
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

    ; host_x = source_tile_x * 5. One 5px source crumb can touch two 8px
    ; destination cells. D = first world column, E = starting pixel phase.
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

; A = scaled world column 0..39. Only the resident future range is marked.
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

; A = scaled world column 0..39. Set the bit for nes_fit_mt_my in that world's
; 16-bit row mask. Bank 1 is restored because the caller continues reading the
; staged queue / authoritative nametable there.
nes_gbc_fit_smb_mark_future_a:
    push bc
    push de
    push hl

    ld c, a
    ld l, a
    ld h, $00
    add hl, hl
    ld de, nes_fit_future_rows
    add hl, de

    ld a, [nes_fit_mt_my]
    cp 8
    jr c, .row_low
    sub 8
    inc hl
.row_low:
    ld e, a
    ld b, $01
    ld a, e
    and a
    jr z, .mask_ready
.mask_loop:
    sla b
    dec a
    jr nz, .mask_loop
.mask_ready:
    ld a, $06
    ldh [rSVBK], a
    ld a, [hl]
    or b
    ld [hl], a
    ld a, $01
    ldh [rSVBK], a

    pop hl
    pop de
    pop bc
    ret

; Publish every unique future FIT cell touched by the just-completed NES NMI.
; There is no background catch-up and no 30-second eventual repair: before the
; transaction is retired, future backing reflects its final authoritative WRAM
; values.  At most the rows actually touched by SMB are composed.
nes_gbc_fit_smb_service_future:
    ld a, [nes_fit_screen]
    and a
    ret z
    ld a, [nes_mirroring]
    cp $01
    ret nz

    ; The MLVs show the future-cell compose can legitimately span several host
    ; frames. The outer VBlank ISR normally masks VBlank during this section,
    ; leaving SCX at the playfield value for those intervening frames; the HUD
    ; therefore jumps horizontally until the long compose finishes.
    ;
    ; Keep the exact same synchronous future publication, but let host VBlank
    ; preempt it. Mark the translated NES NMI as temporarily active so a nested
    ; VBlank takes the existing display-only path: it reapplies HUD SCX/SCY,
    ; rearms the line-28 STAT split, then returns without re-entering BG work.
    ; STAT remains enabled as before. No tile/ring/cache ownership changes here.
    ldh a, [nes_split_active]
    and a
    jr z, .service_begin
    ld a, $01
    ld [nes_nmi_active], a
    ld a, $03                  ; allow VBlank + STAT during long future compose
    ldh [rIE], a
.service_begin:

    ld b, 21
.col_loop:
    ; C = persistent scaled world column for this resident future offset.
    ld a, [nes_fit_origin_mx]
    add b
    cp 40
    jr c, .world_ready
    sub 40
.world_ready:
    ld c, a

    ; Load and atomically clear this world's 15-bit row mask.
    ld l, c
    ld h, $00
    add hl, hl
    ld de, nes_fit_future_rows
    add hl, de

    ld a, $06
    ldh [rSVBK], a
    ld a, [hli]
    ld d, a
    ld a, [hl]
    ld e, a
    xor a
    ld [hl], a
    dec hl
    ld [hl], a
    ld a, $01
    ldh [rSVBK], a

    ld a, d
    or e
    jr z, .next_col

    ; Dirty masks include only rows 2..14. Shift those first two bits away so
    ; D bit0 is row 2, then consume one bit per FIT row.
    srl e
    rr d
    srl e
    rr d

    ld a, b
    ld [nes_fit_mt_mx], a
    ld a, $02
    ld [nes_fit_mt_my], a

.row_loop:
    bit 0, d
    jr z, .row_done
    push bc
    push de
    call nes_video_fit_publish_at_mx_my
    pop de
    pop bc
.row_done:
    srl e
    rr d

    ld a, [nes_fit_mt_my]
    inc a
    ld [nes_fit_mt_my], a
    cp 15
    jr nc, .next_col

    ld a, d
    or e
    jr nz, .row_loop

.next_col:
    inc b
    ld a, b
    cp 32
    jr c, .col_loop

    ld a, $01
    ldh [rSVBK], a

    ; Restore the outer VBlank ISR's original publication state before
    ; returning. Mask nested VBlank first, then clear the temporary NMI guard.
    ldh a, [nes_split_active]
    and a
    ret z
    ld a, $02
    ldh [rIE], a
    xor a
    ld [nes_nmi_active], a
    ret
