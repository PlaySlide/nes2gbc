; GBC video bridge for the virtual NES PPU.
; Correctness-first: expensive operations may wait for VBlank or briefly disable LCD.

SECTION "NES video bridge", ROM0

nes_video_init:
    ; Enter a safe LCD-off setup window once at boot.
    ldh a, [rLCDC]
    bit 7, a
    jr z, .initial_lcd_off
.wait_initial_vblank:
    ldh a, [rLY]
    cp 144
    jr c, .wait_initial_vblank
    ldh a, [rLCDC]
    and $7F
    ldh [rLCDC], a
.initial_lcd_off:

    call nes_upload_chr_bank

    ; LCD is off after upload: clear both BG maps and their CGB attributes.
    xor a
    ldh [rVBK], a
    ld hl, $9800
    ld bc, $0800
    call nes_video_fill_zero

    ld a, $01
    ldh [rVBK], a
    ld hl, $9800
    ld bc, $0800
    call nes_video_fill_zero

    ; GBC maps start cleared, so the published-byte shadow starts cleared too.
    ; Restore bank 1 afterward because virtual NES nametable RAM lives there.
    ld a, $06
    ldh [rSVBK], a
    ld hl, nes_nametable_published_shadow
    ld bc, $0800
    call nes_video_fill_zero
    ld a, $01
    ldh [rSVBK], a

    xor a
    ldh [rVBK], a
    ldh [rSCX], a
    ldh [rSCY], a

    ; Palette 0: white -> light gray -> dark gray -> black.
    ld a, $80
    ldh [rBGPI], a
    ld a, $FF
    ldh [rBGPD], a
    ld a, $7F
    ldh [rBGPD], a
    ld a, $B5
    ldh [rBGPD], a
    ld a, $56
    ldh [rBGPD], a
    ld a, $4A
    ldh [rBGPD], a
    ld a, $29
    ldh [rBGPD], a
    xor a
    ldh [rBGPD], a
    ldh [rBGPD], a

    ; LCD on, BG on, unsigned tile IDs, map $9800.
    ld a, $91
    ldh [rLCDC], a
    ret

nes_video_fill_zero:
.loop:
    xor a
    ld [hli], a
    dec bc
    ld a, b
    or c
    jr nz, .loop
    ret

; Upload selected 8 KiB converted NES CHR bank:
; pattern table $0000 -> CGB VRAM bank 0 $8000
; pattern table $1000 -> CGB VRAM bank 1 $8000
nes_upload_chr_bank:
    PROFILE_INC nes_profile_chr_upload
    ldh a, [rLCDC]
    ld [nes_saved_lcdc], a
    bit 7, a
    jr z, .lcd_off

.wait_vblank:
    ldh a, [rLY]
    cp 144
    jr c, .wait_vblank

    ldh a, [rLCDC]
    and $7F
    ldh [rLCDC], a

.lcd_off:
    ld a, [nes_chr_gbc_bank_base]
    ld b, a
    ld a, [nes_chr_bank]
    add b
    ld [$2000], a

    xor a
    ldh [rVBK], a
    ld hl, $4000
    ld de, $8000
    ld bc, $1000
    call nes_video_copy

    ld a, $01
    ldh [rVBK], a
    ld hl, $5000
    ld de, $8000
    ld bc, $1000
    call nes_video_copy

    xor a
    ldh [rVBK], a

    call nes_restore_code_bank
    ld a, [nes_saved_lcdc]
    ldh [rLCDC], a
    ret

nes_video_copy:
.loop:
    ld a, [hli]
    ld [de], a
    inc de
    dec bc
    ld a, b
    or c
    jr nz, .loop
    ret

; Publish every nametable address staged by the completed translated NES NMI.
; Unlike the earlier queue experiment, this really is atomic from the player's
; point of view: the LCD is disabled during VBlank before the first live-map
; write and is not re-enabled until the whole transaction is copied.
nes_video_flush_nametable_queue_atomic:
    ; Empty queue?
    ld a, [nes_nametable_queue_ptr_hi]
    cp $D8
    jr nz, .has_entries
    ld a, [nes_nametable_queue_ptr_lo]
    and a
    ret z

.has_entries:
    ld a, [nes_diag_event_flags]
    or NES_DIAG_EVENT_QUEUE_FLUSH
    ld [nes_diag_event_flags], a

    ; Start a fresh diagnostic summary for exactly the transaction that is
    ; about to become visible.
    xor a
    ld [nes_ntdiag_tile_count], a
    ld [nes_ntdiag_phys_mask], a
    ld [nes_ntdiag_max_col], a
    ld [nes_ntdiag_first_hi], a
    ld [nes_ntdiag_first_lo], a
    ld [nes_ntdiag_last_hi], a
    ld [nes_ntdiag_last_lo], a
    ld [nes_ntdiag_max_row], a
    ld a, $FF
    ld [nes_ntdiag_min_col], a
    ld [nes_ntdiag_min_row], a

    ; Snapshot which physical GBC nametable is actually being displayed when
    ; this completed NES transaction is published.
    ldh a, [rLCDC]
    and $08
    jr z, .diag_display_map0
    ld a, $01
    jr .diag_display_store
.diag_display_map0:
    xor a
.diag_display_store:
    ld [nes_ntdiag_display_map], a

    ; This routine is called from host VBlank, but a completed NES update may
    ; still take longer than the GBC VBlank window. Keep LCD timing running:
    ; nes_video_sync_nametable_write waits out mode 3 before each VRAM access.
    ; Disabling/re-enabling LCD here resets LY and can make the SMB sprite-0
    ; HUD/playfield STAT split miss an entire frame, producing the repeating
    ; full-screen flash/fixed-background pattern.
    ld a, $01
    ldh [rSVBK], a
    ld de, nes_nametable_queue

.loop:
    ; DE == queue end?
    ld a, [nes_nametable_queue_ptr_hi]
    cp d
    jr nz, .read_entry
    ld a, [nes_nametable_queue_ptr_lo]
    cp e
    jp z, .done

.read_entry:
    ld a, [de]
    inc de
    ld l, a
    ld a, [de]
    inc de
    ld h, a

    ; Record only tile-cell destinations; attribute writes use a different
    ; address geometry and would muddy the column range.
    ld a, h
    and $03
    cp $03
    jr c, .diag_tile
    ld a, l
    cp $C0
    jr nc, .diag_done

.diag_tile:
    ld a, [nes_ntdiag_tile_count]
    and a
    jr nz, .diag_not_first
    ld a, h
    ld [nes_ntdiag_first_hi], a
    ld a, l
    ld [nes_ntdiag_first_lo], a
.diag_not_first:
    ld a, [nes_ntdiag_tile_count]
    inc a
    ld [nes_ntdiag_tile_count], a

    ld a, h
    ld [nes_ntdiag_last_hi], a
    ld a, l
    ld [nes_ntdiag_last_lo], a

    ; Which physical nametable(s) received tile writes?
    ld a, h
    and $04
    jr z, .diag_phys0
    ld a, [nes_ntdiag_phys_mask]
    or $02
    ld [nes_ntdiag_phys_mask], a
    jr .diag_col
.diag_phys0:
    ld a, [nes_ntdiag_phys_mask]
    or $01
    ld [nes_ntdiag_phys_mask], a

.diag_col:
    ld a, l
    and $1F
    ld b, a
    ld a, [nes_ntdiag_min_col]
    cp b
    jr c, .diag_min_done
    jr z, .diag_min_done
    ld a, b
    ld [nes_ntdiag_min_col], a
.diag_min_done:
    ld a, [nes_ntdiag_max_col]
    cp b
    jr nc, .diag_row
    ld a, b
    ld [nes_ntdiag_max_col], a

.diag_row:
    ; row = ((physical-high & 3) << 3) | (low >> 5)
    ld a, h
    and $03
    add a
    add a
    add a
    ld b, a
    ld a, l
    srl a
    srl a
    srl a
    srl a
    srl a
    or b
    ld b, a

    ld a, [nes_ntdiag_min_row]
    cp b
    jr c, .diag_min_row_done
    jr z, .diag_min_row_done
    ld a, b
    ld [nes_ntdiag_min_row], a
.diag_min_row_done:
    ld a, [nes_ntdiag_max_row]
    cp b
    jr nc, .diag_done
    ld a, b
    ld [nes_ntdiag_max_row], a

.diag_done:
    push de
    ld a, [hl]
    call nes_video_sync_nametable_write_if_changed
    pop de
    jp .loop

.done:
    ld a, [nes_ntdiag_commit_serial]
    inc a
    ld [nes_ntdiag_commit_serial], a

    ; Reset transaction before re-enabling scanout.
    xor a
    ld [nes_nametable_queue_ptr_lo], a
    ld [nes_nametable_queue_overflow], a
    ld a, $D8
    ld [nes_nametable_queue_ptr_hi], a

    xor a
    ldh [rVBK], a
    ret

; Wait only while the LCD controller is actively transferring pixels (mode 3).
; VRAM is accessible during HBlank, VBlank, and OAM scan, so do not burn an
; entire frame waiting for LY>=144 for every translated NES PPU write.
nes_video_wait_vram:
    ldh a, [rLCDC]
    bit 7, a
    ret z
.wait:
    ldh a, [rSTAT]
    and $03
    cp $03
    ret nz
    PROFILE_INC nes_profile_vram_wait_block
.wait_busy:
    ldh a, [rSTAT]
    and $03
    cp $03
    jr z, .wait_busy
    ret

; OAM projection is a 160-byte burst, so keep it in the long VBlank window.
nes_video_wait_oam:
    ldh a, [rLCDC]
    bit 7, a
    ret z
.wait:
    ldh a, [rLY]
    cp 144
    ret nc
    PROFILE_INC nes_profile_oam_wait_block
.wait_busy:
    ldh a, [rLY]
    cp 144
    jr c, .wait_busy
    ret

; Input: HL = physical virtual nametable address ($D000-$D7FF),
; A = byte that should be published.  Suppress exact repeats before touching
; live VRAM.  SMB's scrolling NMI often stages addresses whose final value is
; unchanged; the old path still paid mode waits plus tile/attribute writes for
; every one and could occupy scanlines 0-31 before the HUD split.
nes_video_sync_nametable_write_if_changed:
    ld c, a

    ld a, $06
    ldh [rSVBK], a
    ld a, [hl]
    cp c
    jr z, .unchanged

    ld a, c
    ld [hl], a
    ld a, $01
    ldh [rSVBK], a
    ld a, c
    jp nes_video_sync_nametable_write

.unchanged:
    ld a, $01
    ldh [rSVBK], a
    ret

; Input: HL = physical virtual nametable address ($D000-$D7FF), A = written byte.
nes_video_sync_nametable_write:
    PROFILE_INC nes_profile_nametable_sync
    ld c, a

    ; Attribute bytes start at offset $3C0 within each physical 1 KiB table.
    ld a, h
    and $03
    cp $03
    jr c, .tile
    ld a, l
    cp $C0
    jr nc, .attribute

.tile:
    ; While horizontal stitching is active, $9C00 is no longer a pristine
    ; physical NT1 map: it is the synthesized visible playfield.  SMB builds
    ; future columns in the offscreen physical nametable.  Never let an NT1
    ; write touch a stitched column that currently belongs to NT0, even
    ; momentarily; the old write-then-repair path let those future objects
    ; flash for a scanline/frame before repair.
    ld a, [nes_hstitch_valid]
    and a
    jr z, .tile_write
    ld a, [nes_mirroring]
    cp $01
    jr nz, .tile_write
    ldh a, [nes_split_active]
    and a
    jr z, .tile_write

    ; Physical page 0 always updates its $9800 backing map.  Physical page 1
    ; may write $9C00 only when this destination column is actually NT1-owned.
    ld a, h
    and $04
    jr z, .tile_write
    ld a, l
    and $1F
    push bc
    call nes_video_hstitch_source_for_column
    pop bc
    and a
    ret z

.tile_write:
    call nes_video_wait_vram

    ; GBC map high byte is $98 + physical-table/inner-page index.
    ld a, h
    and $07
    add $98
    ld d, a
    ld e, l

    ; Tile ID.
    xor a
    ldh [rVBK], a
    ld a, c
    ld [de], a

    ; Preserve the palette selected by the NES attribute table. A nametable
    ; tile write changes the tile ID, not its 2-bit background palette.
    ;
    ; During an SMB-style stitched split, the two presentation surfaces have
    ; their own captured PPUCTRL state.  $9800 is the fixed top/HUD backing map
    ; and $9C00 is the lower/playfield stitched map.  Using generic nes_ppuctrl
    ; here allowed direct NT1 writes to give otherwise-correct tile IDs the
    ; wrong CGB VRAM-bank bit, producing recognizable ghost digits/scenery
    ; until a later full bank rewrite repaired them.
    ld a, $01
    ldh [rVBK], a
    ld a, [de]
    and $07
    ld b, a

    ld a, [nes_hstitch_valid]
    and a
    jr z, .tile_bank_global
    ld a, [nes_mirroring]
    cp $01
    jr nz, .tile_bank_global
    ldh a, [nes_split_active]
    and a
    jr z, .tile_bank_global

    ld a, d
    cp $9C
    jr z, .tile_bank_bottom
    ldh a, [nes_split_top_ctrl]
    jr .tile_bank_select

.tile_bank_bottom:
    ldh a, [nes_split_bottom_ctrl]
    jr .tile_bank_select

.tile_bank_global:
    ld a, [nes_ppuctrl]

.tile_bank_select:
    and $10
    srl a
    or b
    ld [de], a

    xor a
    ldh [rVBK], a
    call nes_video_stitch_repair_tile_from_page0
    ret

.attribute:
    ld a, c
    jp nes_video_sync_attribute_write

; Keep the synthesized map coherent after a physical nametable tile write.
; If this destination column currently represents physical NT0, copy the
; authoritative expanded map-0 cell into stitched map 1. This both mirrors
; real NT0 changes and repairs any NT1 write that landed in an NT0-owned
; stitched column.
nes_video_stitch_repair_tile_from_page0:
    ld a, [nes_hstitch_valid]
    and a
    ret z
    ld a, [nes_mirroring]
    cp $01
    ret nz
    ldh a, [nes_split_active]
    and a
    ret z

    ld a, l
    and $1F
    call nes_video_hstitch_source_for_column
    and a
    ret nz                       ; source 1: map 1 already has the right cell

    ; Convert virtual D0-D7 inner row bits to same-cell $9800/$9C00 addresses.
    ld a, h
    and $03
    ld b, a
    add $98
    ld h, a
    ld a, b
    add $9C
    ld d, a
    ld e, l

    call nes_video_wait_vram
    xor a
    ldh [rVBK], a
    ld a, [hl]
    ld [de], a

    call nes_video_wait_vram
    ld a, $01
    ldh [rVBK], a

    ; Palette bits come from the authoritative NT0 backing cell, but the
    ; stitched lower playfield's pattern-table bank belongs to bottom_ctrl,
    ; not to whatever transient PPUCTRL value happened to perform the write.
    ld a, [hl]
    and $07
    ld b, a
    ldh a, [nes_split_bottom_ctrl]
    and $10
    srl a
    or b
    ld [de], a

    xor a
    ldh [rVBK], a
    ret

; Expand one NES attribute byte into sixteen CGB tile attributes.
; Input: HL = physical attribute address ($D3C0-$D3FF or $D7C0-$D7FF), A = attribute byte.
nes_video_sync_attribute_write:
    ld b, a

    ; With an active horizontal stitch, map $9C00 is a presentation surface,
    ; not physical NT1 storage.  Route attribute changes through a selective
    ; stitch-aware path so offscreen parser attributes cannot color/bank-swap
    ; currently visible NT0-owned columns.
    ld a, [nes_hstitch_valid]
    and a
    jr z, .physical
    ld a, [nes_mirroring]
    cp $01
    jr nz, .physical
    ldh a, [nes_split_active]
    and a
    jp nz, nes_video_sync_attribute_write_stitched

.physical:
    jp nes_video_sync_attribute_write_physical

; Normal physical-map attribute expansion. Input HL address, B = NES byte.
nes_video_sync_attribute_write_physical:
    ld a, l
    sub $C0
    ld l, a

    ; Attribute row (0-7).
    srl a
    srl a
    srl a
    ld c, a

    ; Destination high = map base ($98/$9C) + row/2.
    ld a, h
    and $04
    add $98
    ld d, a
    ld a, c
    srl a
    add d
    ld d, a

    ; Destination low = (row&1)*$80 + column*4.
    ld a, l
    and $07
    add a
    add a
    ld e, a
    ld a, c
    and $01
    jr z, .dest_ready
    ld a, e
    or $80
    ld e, a
.dest_ready:

    ; Attribute bit 3 selects converted NES pattern table 1 in CGB VRAM bank 1.
    ; When this is the fixed $9800 HUD/backing surface of an active stitched
    ; split, derive the bank from the captured top state rather than whichever
    ; PPUCTRL value happened to be live at publication time.  The stitched
    ; $9C00 path below already uses nes_split_bottom_ctrl explicitly.
    ld a, [nes_hstitch_valid]
    and a
    jr z, .attr_bank_global
    ld a, [nes_mirroring]
    cp $01
    jr nz, .attr_bank_global
    ldh a, [nes_split_active]
    and a
    jr z, .attr_bank_global
    ld a, d
    cp $9C
    jr z, .attr_bank_global
    ldh a, [nes_split_top_ctrl]
    jr .attr_bank_select

.attr_bank_global:
    ld a, [nes_ppuctrl]

.attr_bank_select:
    and $10
    srl a
    ld c, a

    call nes_video_wait_vram
    ld a, $01
    ldh [rVBK], a

    call nes_video_attr_top_row
    call nes_video_wait_vram
    call nes_video_attr_top_row
    call nes_video_wait_vram
    call nes_video_attr_bottom_row
    call nes_video_wait_vram
    call nes_video_attr_bottom_row

    xor a
    ldh [rVBK], a
    ret

; Stitch-aware attribute publication.
; Physical NT0 still updates $9800 because that is the fixed HUD/backing map.
; Physical NT1 remains authoritative in virtual WRAM only while $9C00 is being
; used as the stitched surface.  For the stitched map, update only columns
; whose current owner matches the physical nametable that received the write.
nes_video_sync_attribute_write_stitched:
    ; D = physical source page 0/1.
    ld a, h
    and $04
    jr nz, .source_page1

    xor a
    ld d, a

    ; Keep the real NT0/$9800 attributes current for the fixed HUD.
    push hl
    push bc
    call nes_video_sync_attribute_write_physical
    pop bc
    pop hl
    xor a
    ld d, a
    jr .source_ready

.source_page1:
    ld d, $01

.source_ready:
    ; H = first tile row of this 4x4 attribute cell.
    ; L = first tile column. E = one-past-last column.
    ld a, l
    sub $C0
    ld c, a
    and $07
    add a
    add a
    ld l, a
    add $04
    ld e, a

    ld a, c
    srl a
    srl a
    srl a
    add a
    add a
    ld h, a

.column_loop:
    ; Does this stitched destination column currently come from the physical
    ; nametable that was just changed?
    ld a, l
    push bc
    call nes_video_hstitch_source_for_column
    pop bc
    cp d
    jp nz, .next_column

    ; Preserve source-page/end-column while DE becomes the CGB destination.
    push de

    ; DE = $9C00 + tile_row*32 + tile_column.
    ld a, h
    srl a
    srl a
    srl a
    add $9C
    ld d, a
    ld a, h
    and $07
    swap a
    add a
    or l
    ld e, a

    ; Top two rows: left/right quadrant selected by tile-column bit 1.
    ld a, l
    and $02
    jr z, .top_left
    ld a, b
    srl a
    srl a
    jr .top_mask
.top_left:
    ld a, b
.top_mask:
    and $03
    ld c, a
    ldh a, [nes_split_bottom_ctrl]
    and $10
    srl a
    or c
    ld c, a
    call nes_video_stitch_write_attr_row
    call nes_video_stitch_write_attr_row

    ; Bottom two rows.
    ld a, b
    swap a
    ld c, a
    ld a, l
    and $02
    jr z, .bottom_left
    ld a, c
    srl a
    srl a
    jr .bottom_mask
.bottom_left:
    ld a, c
.bottom_mask:
    and $03
    ld c, a
    ldh a, [nes_split_bottom_ctrl]
    and $10
    srl a
    or c
    ld c, a
    call nes_video_stitch_write_attr_row
    call nes_video_stitch_write_attr_row

    pop de

.next_column:
    inc l
    ld a, l
    cp e
    jp c, .column_loop

    xor a
    ldh [rVBK], a
    ret

nes_video_attr_top_row:
    ld a, b
    and $03
    or c
    ld [de], a
    inc de
    ld [de], a
    inc de

    ld a, b
    srl a
    srl a
    and $03
    or c
    ld [de], a
    inc de
    ld [de], a
    inc de
    jp nes_video_attr_next_row

nes_video_attr_bottom_row:
    ld a, b
    swap a
    and $03
    or c
    ld [de], a
    inc de
    ld [de], a
    inc de

    ld a, b
    swap a
    srl a
    srl a
    and $03
    or c
    ld [de], a
    inc de
    ld [de], a
    inc de
    jp nes_video_attr_next_row

nes_video_attr_next_row:
    ld a, e
    add $1C
    ld e, a
    ret nc
    inc d
    ret

; Synchronize NES background palette RAM into CGB palettes 0-3.
; Input: HL = mapped palette address ($C830-$C84F), A = NES color index.
nes_video_sync_palette_write:
    PROFILE_INC nes_profile_palette_sync
    ld [nes_palette_sync_color], a
    ld a, l
    sub $30
    and $1F
    cp $10
    jr nc, .sprite_palette

    and a
    jr nz, .non_universal

    ; Universal background color occupies color 0 in all four BG palettes.
    ld c, $00
    ld b, $00
    ld a, [nes_palette_sync_color]
    call nes_video_set_bg_color
    ld c, $00
    ld b, $01
    ld a, [nes_palette_sync_color]
    call nes_video_set_bg_color
    ld c, $00
    ld b, $02
    ld a, [nes_palette_sync_color]
    call nes_video_set_bg_color
    ld c, $00
    ld b, $03
    ld a, [nes_palette_sync_color]
    jp nes_video_set_bg_color

.non_universal:
    ld e, a
    and $03
    ret z
    ld c, a
    ld a, e
    srl a
    srl a
    ld b, a
    ld a, [nes_palette_sync_color]
    jp nes_video_set_bg_color

.sprite_palette:
    sub $10
    ld e, a
    and $03
    ret z
    ld c, a
    ld a, e
    srl a
    srl a
    ld b, a
    ld a, [nes_palette_sync_color]
    jp nes_video_set_obj_color

; Input: A = NES color index, B = GBC palette 0-3, C = color 0-3.
; Convert immediately into the HRAM palette shadow. Hardware palette registers
; are updated coherently during host VBlank.
nes_video_set_bg_color:
    ld d, a

    ld a, b
    add a
    add a
    add a
    ld b, a
    ld a, c
    add a
    add b
    ld c, a

    ld a, d
    and $3F
    add a
    ld e, a
    ld d, $00
    ld hl, nes_rgb555_table
    add hl, de
    ld e, [hl]
    inc hl
    ld d, [hl]

    ld a, c
    add LOW(nes_gbc_palette_shadow)
    ld l, a
    ld h, HIGH(nes_gbc_palette_shadow)
    ld a, e
    ld [hli], a
    ld a, d
    ld [hl], a

    ld a, $01
    ldh [nes_palette_dirty], a
    ret

nes_video_set_obj_color:
    ld d, a

    ld a, b
    add a
    add a
    add a
    ld b, a
    ld a, c
    add a
    add b
    add $20
    ld c, a

    ld a, d
    and $3F
    add a
    ld e, a
    ld d, $00
    ld hl, nes_rgb555_table
    add hl, de
    ld e, [hl]
    inc hl
    ld d, [hl]

    ld a, c
    add LOW(nes_gbc_palette_shadow)
    ld l, a
    ld h, HIGH(nes_gbc_palette_shadow)
    ld a, e
    ld [hli], a
    ld a, d
    ld [hl], a

    ld a, $01
    ldh [nes_palette_dirty], a
    ret

; Stream the preconverted 64-byte palette shadow to CGB palette RAM.
; Called only from host VBlank.
nes_video_sync_palette_shadow:
    ld a, [nes_diag_event_flags]
    or NES_DIAG_EVENT_PALETTE_COMMIT
    ld [nes_diag_event_flags], a

    ld hl, nes_gbc_palette_shadow

    ld a, $80
    ldh [rBGPI], a
    ld b, $20
.bg_loop:
    ld a, [hli]
    ldh [rBGPD], a
    dec b
    jr nz, .bg_loop

    ld a, $80
    ldh [rOBPI], a
    ld b, $20
.obj_loop:
    ld a, [hli]
    ldh [rOBPD], a
    dec b
    jr nz, .obj_loop
    ret

nes_rgb555_table:
    dw $3DEF, $7C00, $5C00, $5CA8, $4012, $1014, $0054, $0051
    dw $00CA, $01E0, $01A0, $0160, $2D00, $0000, $0000, $0000
    dw $5EF7, $79E0, $7960, $7D0D, $641A, $2C1C, $00FE, $097C
    dw $01F5, $02C0, $0280, $2280, $4620, $0000, $0000, $0000
    dw $7BDE, $7EE7, $7E2D, $79F2, $79FE, $497E, $2DFE, $227F
    dw $02DE, $0FD6, $2B4B, $4BCB, $6B80, $3DEF, $0000, $0000
    dw $7FFF, $7F94, $7AD6, $7ADA, $7ADE, $5E9E, $573D, $537F
    dw $3F5E, $3FDA, $5BD6, $6BD6, $7FE0, $7B5E, $0000, $0000

; Project up to 40 visible NES sprites into CGB OAM.
; Scan all 64 source entries so composite objects are not chopped merely
; because one of their pieces lives beyond NES OAM entry 39.
nes_video_build_oam_shadow:
    ; Follow-camera tracking consumes raw NES OAM coordinates before viewport
    ; cropping so it can move the crop toward the player rather than merely
    ; following whichever sprites are already visible.
    call nes_view_follow_update

    ld a, [nes_ppuctrl]
    ldh [nes_oam_ppuctrl_tmp], a
    ld hl, nes_oam_ram
    ld de, nes_gbc_oam_shadow
    ld b, 64
    xor a
    ldh [nes_oam_emit_count], a

.scan:
    ; Source Y. NES Y is top minus one.
    ld a, [hli]
    cp $EF
    jp nc, .skip_three_source_bytes
    inc a
    ldh [nes_view_coord_tmp], a
    ldh a, [nes_view_y]
    ld c, a
    ldh a, [nes_view_coord_tmp]
    sub c
    jp c, .skip_three_source_bytes
    cp $90
    jp nc, .skip_three_source_bytes
    add $10
    ldh [nes_oam_proj_y_tmp], a

    ; Save source tile and attributes.
    ld a, [hli]
    ldh [nes_view_sprite_tile_tmp], a
    ld a, [hli]
    ldh [nes_sprite_attr_tmp], a

    ; Source X and viewport crop.
    ld a, [hli]
    ldh [nes_view_coord_tmp], a
    ldh a, [nes_view_x]
    ld c, a
    ldh a, [nes_view_coord_tmp]
    sub c
    jp c, .next_source
    cp $A0
    jp nc, .next_source
    add $08
    ldh [nes_oam_proj_x_tmp], a

    ; Visible sprite: pack it into the next CGB OAM slot.
    ldh a, [nes_oam_proj_y_tmp]
    ld [de], a
    inc de
    ldh a, [nes_oam_proj_x_tmp]
    ld [de], a
    inc de

    ; Tile number and pattern-table bank.
    ldh a, [nes_oam_ppuctrl_tmp]
    bit 5, a
    jr z, .sprite_8x8

    ldh a, [nes_view_sprite_tile_tmp]
    ld c, a
    and $FE
    ld [de], a
    inc de

    bit 0, c
    ld a, $00
    jr z, .bank_ready
    ld a, $08
    jr .bank_ready

.sprite_8x8:
    ldh a, [nes_view_sprite_tile_tmp]
    ld [de], a
    inc de
    ldh a, [nes_oam_ppuctrl_tmp]
    and $08

.bank_ready:
    ldh [nes_sprite_bank_tmp], a

    ; Palette, CGB VRAM bank, priority, H flip, V flip.
    ldh a, [nes_sprite_attr_tmp]
    and $03
    ld c, a
    ldh a, [nes_sprite_bank_tmp]
    or c
    ld c, a

    ldh a, [nes_sprite_attr_tmp]
    bit 5, a
    jr z, .no_priority
    ld a, c
    or $80
    ld c, a
.no_priority:
    ldh a, [nes_sprite_attr_tmp]
    bit 6, a
    jr z, .no_hflip
    ld a, c
    or $20
    ld c, a
.no_hflip:
    ldh a, [nes_sprite_attr_tmp]
    bit 7, a
    jr z, .no_vflip
    ld a, c
    or $40
    ld c, a
.no_vflip:
    ld a, c
    ld [de], a
    inc de

    ldh a, [nes_oam_emit_count]
    inc a
    ldh [nes_oam_emit_count], a
    cp 40
    jp z, .ready

.next_source:
    dec b
    jp nz, .scan
    jr .clear_unused

.skip_three_source_bytes:
    inc hl
    inc hl
    inc hl
    dec b
    jp nz, .scan

.clear_unused:
    ; Hide any CGB OAM slots that were populated on an earlier frame but were
    ; not filled this frame. Y=0 is offscreen; clear the full record for sanity.
    ldh a, [nes_oam_emit_count]
    ld c, a
    ld a, 40
    sub c
    jr z, .ready
    ld b, a
    xor a
.clear_loop:
    ld [de], a
    inc de
    ld [de], a
    inc de
    ld [de], a
    inc de
    ld [de], a
    inc de
    dec b
    jr nz, .clear_loop

.ready:
    ld a, $01
    ldh [nes_oam_shadow_ready], a
    ret

; Copy a fully projected 160-byte shadow into hardware OAM. This routine is
; intentionally tiny so it comfortably completes during host VBlank.
nes_video_sync_oam:
    PROFILE_INC nes_profile_oam_sync
    ld hl, nes_gbc_oam_shadow
    ld de, $FE00
    ld b, $A0
.copy_shadow:
    ld a, [hli]
    ld [de], a
    inc de
    dec b
    jr nz, .copy_shadow
    ret

; NES PPUCTRL bit 4 globally selects BG pattern table $0000/$1000.
; Our CGB representation stores that selection in each tile attribute's VRAM
; bank bit. Synchronize bit 3 deterministically across both maps whenever the
; NES global select changes. Do not XOR: a single stale/mismatched attribute
; would otherwise remain permanently opposite to the rest of the map.
nes_video_toggle_bg_pattern_bank:
    ld a, [nes_diag_event_flags]
    or NES_DIAG_EVENT_BG_BANK_REWRITE
    ld [nes_diag_event_flags], a

    ldh a, [rLCDC]
    bit 7, a
    jr z, .lcd_already_off

    ; LCD may only be disabled safely during VBlank.
    push af
    call nes_video_wait_oam
    pop af

.lcd_already_off:
    push af
    and $7F
    ldh [rLCDC], a

    ; Desired CGB VRAM-bank attribute: NES PPUCTRL.4 -> CGB attr.3.
    ld a, [nes_ppuctrl]
    and $10
    srl a
    ld e, a

    ld a, $01
    ldh [rVBK], a
    ld hl, $9800
    ld bc, $0800
.sync_loop:
    ld a, [hl]
    and $F7
    or e
    ld [hli], a
    dec bc
    ld a, b
    or c
    jr nz, .sync_loop

    xor a
    ldh [rVBK], a
    pop af
    ldh [rLCDC], a
    ret

; Reflect PPUMASK BG/sprite visibility into LCDC bits 0/1.
nes_video_update_mask:
    ldh a, [rLCDC]
    and $FC
    ld b, a

    ld a, [nes_ppumask]
    bit 3, a
    jr z, .no_bg
    ld a, b
    or $01
    ld b, a
.no_bg:
    ld a, [nes_ppumask]
    bit 4, a
    jr z, .mask_store
    ld a, b
    or $02
    ld b, a
.mask_store:
    ld a, b
    ldh [rLCDC], a
    ret

; For vertical mirroring the two physical NES nametables are horizontal
; neighbours. A 256px GBC BG map cannot represent their seamless 512px scroll
; space by simple map selection: when SCX wraps, it would wrap into the same
; NES page. SMB exposes this immediately when gameplay starts scrolling.
;
; During an SMB-style HUD/playfield split, keep $9800 as the fixed HUD/page-0
; surface and synthesize the lower playfield into $9C00. Rebuild only when the
; coarse horizontal key changes or virtual nametable data changed.
nes_video_update_horizontal_stitch:
    ld a, [nes_mirroring]
    cp $01
    jr z, .vertical
.disable:
    xor a
    ld [nes_hstitch_valid], a
    ret

.vertical:
    ldh a, [nes_split_active]
    and a
    jr z, .disable

    ; effective X = lower NES scroll + our crop offset.
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

    ld a, [nes_hstitch_valid]
    and a
    jp z, .full_rebuild

    ld a, [nes_hstitch_key]
    cp c
    ret z
    ld b, a                    ; B = old 0..63 coarse world key
    ld a, c
    ld [nes_hstitch_target_key], a

    ; A translated NES frame can occasionally advance by more than one coarse
    ; tile before the next host presentation. Treat small deltas as catch-up,
    ; not as teleports. The old +/-1-only code fell into full_rebuild for these
    ; skips, disabled LCD, reset LY, and made SMB's split flash periodically.
    ld a, c
    sub b
    and $3F
    cp $09                     ; forward distance 1..8
    jr c, .catchup_forward

    ld a, b
    sub c
    and $3F
    cp $09                     ; backward distance 1..8
    jr c, .catchup_backward

    ; Genuine area transitions can jump much farther.
    jp .full_rebuild

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
    call nes_video_refresh_stitch_column

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
    call nes_video_refresh_stitch_column

    ld a, [nes_hstitch_target_key]
    ld b, a
    ld a, [nes_hstitch_key]
    cp b
    jr nz, .backward_loop
    xor a
    ld [nes_hstitch_dirty], a
    ret

.full_rebuild:
    ld a, [nes_diag_event_flags]
    or NES_DIAG_EVENT_FULL_REBUILD
    ld [nes_diag_event_flags], a

    ld a, [nes_hstitch_full_rebuilds]
    inc a
    ld [nes_hstitch_full_rebuilds], a
    ; Initial activation / discontinuous area jump. A one-time LCD-off rebuild
    ; is acceptable here; steady scrolling never comes through this path.
    ld a, c
    ld [nes_hstitch_key], a

    ldh a, [rLCDC]
    ld [nes_saved_lcdc], a
    and $7F
    ldh [rLCDC], a

    xor a
    ld [nes_hstitch_copy_start], a
.rebuild_columns:
    ld a, [nes_hstitch_copy_start]
    call nes_video_refresh_stitch_column
    ld a, [nes_hstitch_copy_start]
    inc a
    ld [nes_hstitch_copy_start], a
    cp $20
    jr c, .rebuild_columns

    xor a
    ldh [rVBK], a
    ld [nes_hstitch_dirty], a
    ld a, $01
    ld [nes_hstitch_valid], a

    ld a, [nes_saved_lcdc]
    ldh [rLCDC], a
    ret

; Input: A = destination GBC tile column 0..31.
; Refresh one complete stitched column from the authoritative virtual NES
; nametables. This is cheap enough to do while LCD timing remains enabled.
nes_video_refresh_stitch_column:
    and $1F
    ld [nes_hstitch_copy_start], a
    call nes_video_hstitch_source_for_column
    ld [nes_hstitch_copy_len], a      ; 0=physical NT0, 1=physical NT1

    ; CGB attribute bit 3 mirrors the lower/playfield NES pattern-table bit.
    ldh a, [nes_split_bottom_ctrl]
    and $10
    srl a
    ld [nes_hstitch_copy_skip], a

    ld a, $01
    ldh [rSVBK], a

    ; Tile IDs: source D000/D400, destination stitched map $9C00.
    ld a, [nes_hstitch_copy_len]
    and a
    jr z, .tile_source0
    ld h, $D4
    jr .tile_source_ready
.tile_source0:
    ld h, $D0
.tile_source_ready:
    ld a, [nes_hstitch_copy_start]
    ld l, a
    ld d, $9C
    ld e, a
    ld b, $1E                    ; 30 NES tile rows

.tile_loop:
    ld a, [hl]
    ld c, a
    call nes_video_wait_vram
    xor a
    ldh [rVBK], a
    ld a, c
    ld [de], a

    ld a, l
    add $20
    ld l, a
    jr nc, .tile_h_ok
    inc h
.tile_h_ok:
    ld a, e
    add $20
    ld e, a
    jr nc, .tile_d_ok
    inc d
.tile_d_ok:
    dec b
    jr nz, .tile_loop

    ; CGB attributes for this column. One NES attribute byte covers 4x4 tiles.
    ld a, [nes_hstitch_copy_len]
    and a
    jr z, .attr_source0
    ld h, $D7
    jr .attr_source_ready
.attr_source0:
    ld h, $D3
.attr_source_ready:
    ld a, [nes_hstitch_copy_start]
    srl a
    srl a
    add $C0
    ld l, a

    ld d, $9C
    ld a, [nes_hstitch_copy_start]
    ld e, a
    ld b, $08                    ; eight 4-row attribute bands

.attr_group:
    ; Top two rows of the 4x4 attribute cell.
    ld a, [hl]
    ld c, a
    ld a, [nes_hstitch_copy_start]
    and $02
    jr z, .attr_top_left
    ld a, c
    srl a
    srl a
    jr .attr_top_mask
.attr_top_left:
    ld a, c
.attr_top_mask:
    and $03
    ld c, a
    ld a, [nes_hstitch_copy_skip]
    or c
    ld c, a
    call nes_video_stitch_write_attr_row
    call nes_video_stitch_write_attr_row

    ; Bottom two rows.
    ld a, [hl]
    swap a
    ld c, a
    ld a, [nes_hstitch_copy_start]
    and $02
    jr z, .attr_bottom_left
    ld a, c
    srl a
    srl a
    jr .attr_bottom_mask
.attr_bottom_left:
    ld a, c
.attr_bottom_mask:
    and $03
    ld c, a
    ld a, [nes_hstitch_copy_skip]
    or c
    ld c, a
    call nes_video_stitch_write_attr_row
    call nes_video_stitch_write_attr_row

    ld a, l
    add $08
    ld l, a
    jr nc, .attr_h_ok
    inc h
.attr_h_ok:
    dec b
    jr nz, .attr_group

    xor a
    ldh [rVBK], a
    ret

; Return A=0/1 for the physical NES nametable currently assigned to a GBC
; destination column under nes_hstitch_key. Clobbers B/C.
nes_video_hstitch_source_for_column:
    and $1F
    ld [nes_hstitch_copy_start], a
    ld a, [nes_hstitch_key]
    and $1F
    ld b, a                      ; coarse seam column q

    ld a, [nes_hstitch_key]
    and $20
    jr z, .source_base0
    ld c, $01
    jr .source_base_ready
.source_base0:
    ld c, $00
.source_base_ready:
    ld a, [nes_hstitch_copy_start]
    cp b
    jr nc, .source_same_page
    ld a, c
    xor $01
    ret
.source_same_page:
    ld a, c
    ret

; Write C to stitched-map CGB attributes at DE and advance one tile row.
nes_video_stitch_write_attr_row:
    call nes_video_wait_vram
    ld a, $01
    ldh [rVBK], a
    ld a, c
    ld [de], a
    ld a, e
    add $20
    ld e, a
    ret nc
    inc d
    ret

; Split-aware map selection. In vertical-mirroring games, map 0 remains the
; fixed HUD surface and map 1 is the stitched scrolling playfield.
nes_video_apply_split_top_map:
    ld a, [nes_mirroring]
    cp $01
    jr nz, .normal
    ldh a, [rLCDC]
    and $F7
    ldh [rLCDC], a
    ret
.normal:
    ldh a, [nes_split_armed_top_ctrl]
    jp nes_video_apply_map_select_a

nes_video_apply_split_bottom_map:
    ld a, [nes_mirroring]
    cp $01
    jr nz, .normal
    ldh a, [rLCDC]
    or $08
    ldh [rLCDC], a
    ret
.normal:
    ldh a, [nes_split_armed_ctrl]
    jp nes_video_apply_map_select_a

; Apply only the NES base-nametable selection from PPUCTRL in A.
; This is used by raster splits so the HUD and playfield may select different
; mirrored NES nametables without disturbing global sprite-size state.
nes_video_apply_map_select_a:
    ld c, a
    ldh a, [rLCDC]
    and $F7
    ld b, a

    ld a, [nes_mirroring]
    cp $01
    jr z, .map_vertical

    ; Horizontal mirroring: physical table comes from PPUCTRL bit 1.
    ld a, c
    and $02
    jr z, .map_store
    ld a, b
    or $08
    jr .map_write

.map_vertical:
    ; Vertical mirroring: physical table comes from PPUCTRL bit 0.
    ld a, c
    and $01
    jr z, .map_store
    ld a, b
    or $08
    jr .map_write

.map_store:
    ld a, b
.map_write:
    ldh [rLCDC], a
    ret

; Present one ordinary NES scroll pair through a 160x144 GBC crop.
; NES vertical nametables are 240 pixels high, while a CGB BG map wraps at
; 256 pixels. If the visible crop crosses NES Y=240, arm a one-shot STAT split
; that toggles the vertical nametable and adds 16 to SCY at the exact seam.
nes_video_apply_single_scroll:
    ; Horizontal crop remains a simple 256-pixel wrap.
    ld a, [nes_ppu_scroll_x]
    ld b, a
    ldh a, [nes_view_x]
    add b
    ldh [rSCX], a

    ; Compute 9-bit y_total = NES scroll Y + crop Y.
    ld a, [nes_ppu_scroll_y]
    ld b, a
    ldh a, [nes_view_y]
    add b
    ld d, a
    ld e, $00
    jr nc, .sum_ready
    inc e
.sum_ready:

    ; y_total >= 240 means the crop already starts in the vertically adjacent
    ; nametable. GBC SCY must be y_total + 16 so its 256-pixel wrap lines up
    ; with the NES 240-pixel wrap.
    ld a, e
    and a
    jr nz, .top_wrapped
    ld a, d
    cp $F0
    jr nc, .top_wrapped

    ; Top of crop is still in the base nametable.
    ld a, [nes_ppuctrl]
    ldh [nes_seam_top_ctrl], a
    call nes_video_apply_map_select_a
    ld a, d
    ldh [nes_seam_top_y], a
    ldh [rSCY], a

    ; A 144-line crop crosses Y=240 iff y_total >= 97.
    cp $61
    jr c, .no_seam

    ; Split line = 240 - y_total.
    ld a, $F0
    sub d
    ldh [nes_seam_line], a
    ldh [rLYC], a

    ; Below the split use the vertically adjacent logical nametable and
    ; compensate the CGB's extra 16 map pixels.
    ldh a, [nes_seam_top_ctrl]
    xor $02
    ldh [nes_seam_bottom_ctrl], a
    ld a, d
    add $10
    ldh [nes_seam_bottom_y], a

    ld a, $01
    ldh [nes_seam_active], a
    ldh a, [rSTAT]
    or $40
    ldh [rSTAT], a
    ret

.top_wrapped:
    ld a, [nes_ppuctrl]
    xor $02
    ldh [nes_seam_top_ctrl], a
    call nes_video_apply_map_select_a
    ld a, d
    add $10
    ldh [nes_seam_top_y], a
    ldh [rSCY], a

.no_seam:
    xor a
    ldh [nes_seam_active], a
    ldh a, [rSTAT]
    and $BF
    ldh [rSTAT], a
    ret

; Re-arm a vertical seam for another host frame using the last completed NES
; display state. Needed because the STAT source is deliberately one-shot.
nes_video_rearm_vertical_seam:
    ldh a, [nes_seam_top_ctrl]
    call nes_video_apply_map_select_a
    ldh a, [nes_seam_top_y]
    ldh [rSCY], a
    ldh a, [nes_seam_line]
    ldh [rLYC], a
    ldh a, [rSTAT]
    or $40
    ldh [rSTAT], a
    ret

; Reflect NES base-nametable selection and sprite size into GBC LCDC.
nes_video_update_ctrl:
    ld a, [nes_diag_event_flags]
    or NES_DIAG_EVENT_CTRL_COMMIT
    ld [nes_diag_event_flags], a

    ldh a, [rLCDC]
    and $F3
    ld b, a

    ld a, [nes_ppuctrl]
    bit 5, a
    jr z, .size_done
    ld a, b
    or $04
    ld b, a
.size_done:

    ld a, [nes_mirroring]
    cp $01
    jr z, .vertical

    ; Horizontal mirroring: physical table comes from PPUCTRL bit 1.
    ld a, [nes_ppuctrl]
    and $02
    jr z, .store
    ld a, b
    or $08
    jr .write

.vertical:
    ; Vertical mirroring: physical table comes from PPUCTRL bit 0.
    ld a, [nes_ppuctrl]
    and $01
    jr z, .store
    ld a, b
    or $08
    jr .write

.store:
    ld a, b
.write:
    ldh [rLCDC], a
    ret
