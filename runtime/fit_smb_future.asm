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
; each scaled world column. Keep an authoritative shadow of the 128 NES
; attribute bytes so FIT can repair palette ownership without recomposing tiles.
SECTION "FIT SMB future backing state", WRAMX[$D900], BANK[6]
nes_fit_future_rows: ds $50
nes_fit_attr_shadow: ds $80
nes_fit_attr_shadow_valid: ds 1

SECTION "FIT SMB future backing", ROM0

nes_gbc_fit_smb_future_init:
    ld a, $06
    ldh [rSVBK], a
    xor a
    ld hl, nes_fit_future_rows
    ld b, $D1
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

; SMB's active FIT queue deliberately skips NES attribute entries: sending one
; through the generic FIT dirty path would request a whole resident-page rebuild.
; Track the authoritative 128 attribute bytes instead and repair only palette
; bits for cells whose *center source tile* belongs to the changed attribute.
; This is the exact same ownership rule used by nes_video_fit_publish_at_mx_my.
nes_gbc_fit_smb_sync_attributes:
    ldh a, [nes_split_active]
    and a
    ret z

    ld a, $06
    ldh [rSVBK], a
    ld a, [nes_fit_attr_shadow_valid]
    and a
    jr nz, .scan

    ; First established split: seed the shadow. Existing FIT cells were already
    ; composed from authoritative attribute RAM, so there is nothing to repair.
    ld a, $01
    ld [nes_fit_attr_shadow_valid], a
    ldh [rSVBK], a

    ld hl, $D3C0
    ld de, nes_fit_attr_shadow
    ld b, $40
.seed0:
    ld a, [hli]
    ld [nes_fit_mt_tmp_l], a
    ld a, $06
    ldh [rSVBK], a
    ld a, [nes_fit_mt_tmp_l]
    ld [de], a
    inc de
    ld a, $01
    ldh [rSVBK], a
    dec b
    jr nz, .seed0

    ld hl, $D7C0
    ld b, $40
.seed1:
    ld a, [hli]
    ld [nes_fit_mt_tmp_l], a
    ld a, $06
    ldh [rSVBK], a
    ld a, [nes_fit_mt_tmp_l]
    ld [de], a
    inc de
    ld a, $01
    ldh [rSVBK], a
    dec b
    jr nz, .seed1
    ret

.scan:
    ld a, $01
    ldh [rSVBK], a
    ld hl, $D3C0
    ld de, nes_fit_attr_shadow
    xor a
    ld [nes_fit_mt_page], a
    ld c, $00
    call .scan_page

    ld hl, $D7C0
    ld de, nes_fit_attr_shadow + $40
    ld a, $01
    ld [nes_fit_mt_page], a
    ld c, $00
    call .scan_page

    ld a, $01
    ldh [rSVBK], a
    ret

.scan_page:
    ld b, $40
.scan_loop:
    ld a, $01
    ldh [rSVBK], a
    ld a, [hl]
    ld [nes_fit_mt_tmp_l], a

    ld a, $06
    ldh [rSVBK], a
    push bc
    ld a, [de]
    ld b, a
    ld a, [nes_fit_mt_tmp_l]
    cp b
    jr z, .same
    ld [de], a
    pop bc

    ld a, $01
    ldh [rSVBK], a
    push bc
    push de
    push hl
    ld a, c
    call nes_gbc_fit_smb_refresh_attribute_a
    pop hl
    pop de
    pop bc
    jr .next

.same:
    pop bc
.next:
    inc hl
    inc de
    inc c
    dec b
    jr nz, .scan_loop
    ret

; A = physical NES attribute index 0..63, nes_fit_mt_page = NT0/NT1.
; Vertical ownership is exact: each 4-source-tile attribute row maps to two FIT
; rows because the center source row is my*2+1. Attribute row 0 is the fixed HUD
; surface and stays untouched by playfield maintenance.
nes_gbc_fit_smb_refresh_attribute_a:
    ld d, a
    srl a
    srl a
    srl a
    add a
    cp 2
    ret c
    cp 15
    ret nc
    ld [nes_fit_mt_my], a

    ; Horizontal ownership must follow the compositor's CENTER source tile, not
    ; geometric overlap. For one 160px NES page the 8 attribute columns own:
    ;   starts 0,2,5,7,10,12,15,17 and counts 2,3,2,3,2,3,2,3.
    ; The previous blanket "three columns" rule painted neighbouring cells with
    ; the wrong palette, which is exactly why ghosts returned while only about
    ; half of the green strips disappeared.
    ld a, d
    and $07
    ld e, a
    ld d, $00
    ld hl, nes_gbc_fit_smb_attr_start
    add hl, de
    ld a, [hl]
    ld b, a
    ld hl, nes_gbc_fit_smb_attr_count
    add hl, de
    ld a, [hl]
    ld [nes_fit_mt_quad + 3], a

    ld a, [nes_fit_mt_page]
    and a
    jr z, .page_ready
    ld a, b
    add 20
    ld b, a
.page_ready:
    ld a, b
    ld [nes_fit_mt_quad + 2], a

    call nes_gbc_fit_smb_refresh_attribute_span

    ld a, [nes_fit_mt_my]
    inc a
    cp 15
    ret nc
    ld [nes_fit_mt_my], a
    jp nes_gbc_fit_smb_refresh_attribute_span

nes_gbc_fit_smb_refresh_attribute_span:
    ld a, [nes_fit_mt_quad + 2]
    ld b, a
    ld a, [nes_fit_mt_quad + 3]
    ld c, a
.loop:
    push bc
    ld a, b
    call nes_gbc_fit_smb_refresh_palette_world_a
    pop bc
    inc b
    dec c
    jr nz, .loop
    ret

; A = scaled world column 0..39. Palette-repair only resident ring cells. Cells
; outside the 32-column ring will acquire the authoritative palette normally
; when recycled into backing later.
nes_gbc_fit_smb_refresh_palette_world_a:
    ld b, a
    ld a, [nes_fit_origin_mx]
    ld c, a
    ld a, b
    sub c
    jr nc, .delta_ready
    add 40
.delta_ready:
    cp 32
    ret nc
    ld [nes_fit_mt_mx], a
    jp nes_gbc_fit_smb_refresh_palette_cell

; Refresh only CGB palette bits for one resident FIT cell. Preserve tile number,
; pixel data, pattern-bank bit, and all ring ownership.
nes_gbc_fit_smb_refresh_palette_cell:
    call nes_video_fit_world_col
    add a
    ld e, a
    ld d, $00
    ld hl, nes_video_fit_wide_x_table
    add hl, de
    ld a, [hli]
    ld b, a
    ld a, [hl]
    and a
    jr z, .center_ready
    inc b
.center_ready:
    ld a, b
    and $3F
    ld b, a
    ld a, [nes_fit_mt_my]
    add a
    inc a
    ld [nes_fit_mt_tmp_h], a
    ld a, b
    call nes_video_fit_read_nt_tile_a
    call nes_video_authoritative_tile_palette
    and $07
    ld [nes_fit_mt_tmp_h], a

    ld a, [nes_fit_vram_page]
    and $F8
    srl a
    srl a
    srl a
    ld b, a
    ld a, [nes_fit_mt_mx]
    add b
    and $1F
    call nes_video_fit_map_addr_a

    call nes_video_wait_vram
    ld a, $01
    ldh [rVBK], a
    ld a, [de]
    and $F8
    ld b, a
    ld a, [nes_fit_mt_tmp_h]
    or b
    ld [de], a
    xor a
    ldh [rVBK], a
    ld a, $01
    ldh [rSVBK], a
    ret

nes_gbc_fit_smb_attr_start:
    db 0, 2, 5, 7, 10, 12, 15, 17
nes_gbc_fit_smb_attr_count:
    db 2, 3, 2, 3, 2, 3, 2, 3

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

    ; Palette-only reconciliation. This never recomposes pattern data and uses
    ; the same center-source ownership rule as normal FIT publication.
    call nes_gbc_fit_smb_sync_attributes

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
