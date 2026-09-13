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
    ; Fit-screen already installed identity maps during CHR upload — skip wipe.
    ld a, [nes_fit_screen]
    and a
    jr nz, .maps_ready

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

.maps_ready:

    ; GBC maps start cleared, so the published-byte shadow starts cleared too.
    ; Restore bank 1 afterward because virtual NES nametable RAM lives there.
    ld a, $06
    ldh [rSVBK], a
    ld hl, nes_nametable_published_shadow
    ld bc, $0800
    call nes_video_fill_zero

    ; The SMB-only staging bitmap is also zeroed once here. Ordinary games
    ; never touch it, so they no longer need a 256-byte clear on every NMI.
    ld hl, nes_nametable_stage_seen
    ld bc, $0100
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

    ld a, [nes_fit_screen]
    and a
    jr nz, .fit_chr_upload

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
    jr .chr_upload_done

.fit_chr_upload:
    ; Bank 0 = composed BG metatiles. Bank 1 = active sprite PT (half-CHR).
    call nes_video_fit_upload_sprite_chr
    call nes_video_fit_init_identity

.chr_upload_done:
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

IF DEF(NES2GBC_DEBUG_TRACE)
    ; Start a fresh diagnostic summary for exactly the transaction that is
    ; about to become visible. TRACE builds only — release used to pay this
    ; (and the per-tile update below) on every published nametable byte.
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
ENDC

    ; Keep the physical LCD running while publishing the staged transaction.
    ; Start with the VBlank fast path; if publication runs past VBlank,
    ; nes_video_wait_vram falls back to waiting around mode 3 safely.
    ; Never disable LCD here: real GBC hardware visibly flashes white when
    ; LCDC.7 is cleared, even when the transition begins during VBlank.
    ; Prefer unlocked VRAM writes while this host VBlank still owns the bus.
    ld a, $01
    ld [nes_vram_unlocked], a
    ldh [rSVBK], a

    ; Fit: large NT floods must not compose per queue entry (Balloon Fight hang).
    ; Pre-mark dirty for one chunked full-page catch-up at .done. Small updates
    ; (SMB scroll columns) stay incremental in the loop below.
    ld a, [nes_fit_screen]
    and a
    jr z, .fit_bulk_done
    ld a, [nes_nametable_queue_ptr_hi]
    cp $D8
    jr nz, .fit_bulk
    ld a, [nes_nametable_queue_ptr_lo]
    cp 96                      ; >= 48 tile entries (2 bytes each)
    jr c, .fit_bulk_done
.fit_bulk:
    ld a, $01
    ld [nes_fit_dirty], a
    xor a
    ld [nes_fit_recompose_my], a
.fit_bulk_done:

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

IF DEF(NES2GBC_DEBUG_TRACE)
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
ENDC
    push de
    ld a, [hl]

    ; Persistent-value suppression is specific to the synthesized SMB stitched
    ; map. Ordinary physical maps have side effects such as vertical seam
    ; padding, so a staged generic write must run the full publication path
    ; even when the NES byte itself happens to match the previous value.
    ld b, a
    ld a, [nes_hstitch_valid]
    and a
    jr z, .publish_generic


    ld a, b
    call nes_video_sync_nametable_write_if_changed
    jr .publish_done

.publish_generic:
    ld a, b
    call nes_video_sync_nametable_write

.publish_done:
    pop de
    jp .loop

.done:
    ; Fit-screen: coalesce all staged NT/attr touches into one resident-page
    ; recompose while VRAM is still unlocked. Per-byte compose was hanging
    ; Balloon Fight on full-screen fills and corrupting SMB once scroll queued
    ; column updates.
    ld a, [nes_fit_screen]
    and a
    call nz, nes_video_fit_flush_dirty

    xor a
    ld [nes_vram_unlocked], a
IF DEF(NES2GBC_DEBUG_TRACE)
    ld a, [nes_ntdiag_commit_serial]
    inc a
    ld [nes_ntdiag_commit_serial], a
ENDC

    ; Reset the completed transaction. Scanout stayed enabled throughout.
    xor a
    ld [nes_nametable_queue_ptr_lo], a
    ld [nes_nametable_queue_overflow], a
    ld a, $D8
    ld [nes_nametable_queue_ptr_hi], a

    xor a
    ldh [rVBK], a
    ret

; Rebuild both physical GBC background maps from authoritative NES
; nametable WRAM.  This is a correctness checkpoint used when an ordinary game
; finishes a rendering-off screen construction and re-enables the background.
; It tells us whether incremental publication has drifted from virtual NES state.
nes_video_rebuild_generic_maps_atomic:
    ldh a, [rLCDC]
    ld [nes_saved_lcdc], a
    bit 7, a
    jr z, .rebuild_lcd_off

    ; LCD may only be disabled during VBlank.
    call nes_video_wait_oam
    ldh a, [rLCDC]
    and $7F
    ldh [rLCDC], a

.rebuild_lcd_off:
    ld a, [nes_fit_screen]
    and a
    jr z, .rebuild_generic

    ; Fit: one resident recompose instead of 2048 per-byte composes.
    ld a, $01
    ld [nes_fit_dirty], a
    ld [nes_vram_unlocked], a
    call nes_video_fit_flush_dirty
    xor a
    ld [nes_vram_unlocked], a
    jr .rebuild_fit_done

.rebuild_generic:
    ld a, $01
    ldh [rSVBK], a
    ld hl, $D000

.rebuild_loop:
    ld a, [hl]
    push hl
    call nes_video_sync_nametable_write
    pop hl
    inc hl
    ld a, h
    cp $D8
    jr nz, .rebuild_loop

.rebuild_fit_done:

    ; The rebuilt attributes used the current global PPUCTRL.4, so make that
    ; state the committed baseline and avoid an immediate redundant full-bank
    ; rewrite after LCD is restored.
    ld a, [nes_ppuctrl]
    and $10
    srl a
    ld [nes_bg_pattern_committed], a

    ld a, $01
    ldh [rSVBK], a
    xor a
    ldh [rVBK], a

    ld a, [nes_saved_lcdc]
    ldh [rLCDC], a
    ret

; Wait only while the LCD controller is actively transferring pixels (mode 3).
; VRAM is accessible during HBlank, VBlank, and OAM scan, so do not burn an
; entire frame waiting for LY>=144 for every translated NES PPU write.
nes_video_wait_vram:
    ; Host publish can set nes_vram_unlocked while still in VBlank. Skip the
    ; STAT poll until scanout resumes (LY < 144), then fall back to waiting.
    ld a, [nes_vram_unlocked]
    and a
    jr z, .locked
    ldh a, [rLY]
    cp 144
    ret nc
    xor a
    ld [nes_vram_unlocked], a
.locked:
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

    ld a, [nes_fit_screen]
    and a
    ld a, c
    jp nz, nes_video_fit_sync_nametable_write

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

    ; A tile write normally preserves the NES attribute palette. That is fine
    ; for ordinary physical maps, but a stitched destination cell is reused as
    ; the 512px world scrolls. Preserving the *old CGB cell* palette can leave a
    ; question/coin palette attached to unrelated scenery after ownership
    ; changes. Recompute stitched palette bits from authoritative NES attribute
    ; RAM for this exact source tile instead.
    ld a, $01
    ldh [rVBK], a

    ld a, [nes_hstitch_valid]
    and a
    jr z, .tile_attr_preserve
    ld a, [nes_mirroring]
    cp $01
    jr nz, .tile_attr_preserve
    ldh a, [nes_split_active]
    and a
    jr z, .tile_attr_preserve

    push bc
    push de
    push hl
    call nes_video_authoritative_tile_palette
    pop hl
    pop de
    pop bc
    ld b, a

    ; The stitched playfield's pattern-table bank belongs to the captured
    ; bottom raster state, not to a transient live PPUCTRL write.
    ldh a, [nes_split_bottom_ctrl]
    and $10
    srl a
    or b
    ld b, a
    jr .tile_attr_store

.tile_attr_preserve:
    ld a, [de]
    and $07
    ld b, a
    ld a, [nes_ppuctrl]
    and $10
    srl a
    or b
    ld b, a

.tile_attr_store:
    ld a, b
    ld [de], a

    xor a
    ldh [rVBK], a

    ; NES nametables have only 30 tile rows. Keep the two otherwise-unused
    ; GBC rows 30-31 populated from the vertically adjacent NES nametable for
    ; ordinary single-scroll games. Normally the synthetic Y=240 raster seam
    ; switches maps before these rows are scanned; if a long VBlank commit
    ; delays that switch, the padding shows correct adjacent content instead
    ; of tile-0/attribute garbage.
    call nes_video_mirror_vertical_seam_tile_safe
    call nes_video_stitch_repair_tile_from_page0
    ret

.attribute:
    ld a, c
    jp nes_video_sync_attribute_write

; Input: HL = authoritative physical NES tile address ($D000-$D7BF).
; Output: A = NES 2-bit background palette for that exact tile.
; Clobbers B/C/D/E/HL. Caller preserves anything it still needs.
nes_video_authoritative_tile_palette:
    ; Keep the original low byte: bits 0-4 are tile column, bit 6 selects the
    ; lower half of a 4-row attribute cell, bit 7 contributes to attribute row.
    ld c, l

    ; E = attribute column (tile column / 4).
    ld a, l
    and $1F
    srl a
    srl a
    ld e, a

    ; D = attribute row. NES tile row =
    ; ((physical-high & 3) * 8) + (low / 32), so row/4 simplifies to
    ; ((physical-high & 3) * 2) + bit7(low).
    ld a, h
    and $03
    add a
    ld d, a
    ld a, c
    and $80
    rlca
    add d

    ; L = $C0 + attribute_row*8 + attribute_column.
    add a
    add a
    add a
    add e
    add $C0
    ld l, a

    ; H = $D3/$D7 attribute page matching the source physical nametable.
    ld a, h
    and $04
    add $D3
    ld h, a

    ld a, [hl]
    ld b, a

    ; Vertical quadrant: tile rows 0-1 use low nibble, rows 2-3 high nibble.
    bit 6, c
    jr z, .auth_attr_top
    ld a, b
    swap a
    ld b, a
.auth_attr_top:

    ; Horizontal quadrant: tile columns 0-1 use low pair, 2-3 high pair.
    bit 1, c
    jr z, .auth_attr_left
    ld a, b
    srl a
    srl a
    ld b, a
.auth_attr_left:
    ld a, b
    and $03
    ret

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
    ld a, [nes_ppuctrl]
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
    call nes_video_mirror_vertical_seam_attr_safe
    ret

; Populate GBC padding rows 30-31 for the ordinary single-scroll
; vertical-seam path. Do nothing for game-authored raster splits or the SMB
; horizontal stitched presentation map.
; Input: HL = physical NES nametable tile address, C = tile ID, B = CGB attr.
nes_video_mirror_vertical_seam_tile_safe:
    ldh a, [nes_split_active]
    and a
    ret nz
    ld a, [nes_hstitch_valid]
    and a
    ret nz

    ; Only source NES tile rows 0 and 1 feed GBC padding rows 30 and 31.
    ld a, h
    and $03
    ret nz
    ld a, l
    cp $40
    ret nc

    ; Horizontal mirroring: vertical adjacency changes physical table.
    ; Vertical mirroring: vertical adjacency repeats the same physical table.
    ld a, [nes_mirroring]
    cp $01
    jr z, .tile_same_table

    ld a, h
    and $04
    ld d, $9F
    jr z, .tile_dest_ready
    ld d, $9B
    jr .tile_dest_ready

.tile_same_table:
    ld a, h
    and $04
    ld d, $9B
    jr z, .tile_dest_ready
    ld d, $9F

.tile_dest_ready:
    ld a, l
    add $C0
    ld e, a

    call nes_video_wait_vram
    xor a
    ldh [rVBK], a
    ld a, c
    ld [de], a

    call nes_video_wait_vram
    ld a, $01
    ldh [rVBK], a
    ld a, b
    ld [de], a

    xor a
    ldh [rVBK], a
    ret

; Mirror palette attributes for source NES tile rows 0-1 into the same safety
; rows. Input after physical attribute expansion: H=$D3/$D7, L=index $00-$3F.
nes_video_mirror_vertical_seam_attr_safe:
    ldh a, [nes_split_active]
    and a
    ret nz
    ld a, [nes_hstitch_valid]
    and a
    ret nz

    ld a, l
    cp $08
    ret nc

    ; Source CGB map.
    ld a, h
    and $04
    ld b, $98
    jr z, .attr_source_ready
    ld b, $9C
.attr_source_ready:

    ; Destination padding map follows NES vertical adjacency.
    ld a, [nes_mirroring]
    cp $01
    jr z, .attr_same_table

    ld a, h
    and $04
    ld c, $9F
    jr z, .attr_dest_ready
    ld c, $9B
    jr .attr_dest_ready

.attr_same_table:
    ld a, h
    and $04
    ld c, $9B
    jr z, .attr_dest_ready
    ld c, $9F

.attr_dest_ready:
    ld a, l
    and $07
    add a
    add a
    ld l, a
    ld e, a
    ld h, b
    ld d, c
    ld a, e
    add $C0
    ld e, a

    call nes_video_wait_vram
    ld a, $01
    ldh [rVBK], a

    ; Source row 0 -> padding row 30.
    ld a, [hli]
    ld [de], a
    inc de
    ld a, [hli]
    ld [de], a
    inc de
    ld a, [hli]
    ld [de], a
    inc de
    ld a, [hli]
    ld [de], a

    ; Advance both pointers to row 1 / padding row 31.
    ld a, l
    add $1C
    ld l, a
    jr nc, .attr_src_row1_ready
    inc h
.attr_src_row1_ready:
    ld a, e
    add $1D
    ld e, a
    jr nc, .attr_dst_row1_ready
    inc d
.attr_dst_row1_ready:

    call nes_video_wait_vram
    ld a, $01
    ldh [rVBK], a
    ld a, [hli]
    ld [de], a
    inc de
    ld a, [hli]
    ld [de], a
    inc de
    ld a, [hli]
    ld [de], a
    inc de
    ld a, [hl]
    ld [de], a

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
    ; Fit-screen mode already maps the full NES frame; skip follow/crop.
    ld a, [nes_fit_screen]
    and a
    call z, nes_view_follow_update

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

    ld a, [nes_fit_screen]
    and a
    jr nz, .fit_y

    ldh a, [nes_view_y]
    ld c, a
    ldh a, [nes_view_coord_tmp]
    sub c
    jp c, .skip_three_source_bytes
    cp $90
    jp nc, .skip_three_source_bytes
    add $10
    ldh [nes_oam_proj_y_tmp], a
    jr .y_ready

.fit_y:
    ; Half-scale + vertical letterbox: (144-120)/2 = 12.
    ldh a, [nes_view_coord_tmp]
    srl a
    add 12
    cp $90
    jp nc, .skip_three_source_bytes
    add $10
    ldh [nes_oam_proj_y_tmp], a

.y_ready:
    ; Save source tile and attributes.
    ld a, [hli]
    ldh [nes_view_sprite_tile_tmp], a
    ld a, [hli]
    ldh [nes_sprite_attr_tmp], a

    ; Source X.
    ld a, [hli]
    ldh [nes_view_coord_tmp], a

    ld a, [nes_fit_screen]
    and a
    jr nz, .fit_x

    ldh a, [nes_view_x]
    ld c, a
    ldh a, [nes_view_coord_tmp]
    sub c
    jp c, .next_source
    cp $A0
    jp nc, .next_source
    add $08
    ldh [nes_oam_proj_x_tmp], a
    jr .x_ready

.fit_x:
    ; Half-scale + horizontal letterbox: (160-128)/2 = 16.
    ldh a, [nes_view_coord_tmp]
    srl a
    add 16
    cp $A0
    jp nc, .next_source
    add $08
    ldh [nes_oam_proj_x_tmp], a

.x_ready:

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
    ; Fit-screen: sprite CHR lives only in VRAM bank 1.
    ld c, a
    ld a, [nes_fit_screen]
    and a
    ld a, c
    jr z, .bank_store
    ld a, $08
.bank_store:
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
    ld b, 40

    ; One loop per complete CGB OAM entry. E runs only $00-$9F, so INC E is
    ; sufficient and cheaper than INC DE. Reducing loop branches as well cuts
    ; the publication cost substantially without changing OAM contents.
.copy_shadow:
    ld a, [hli]
    ld [de], a
    inc e
    ld a, [hli]
    ld [de], a
    inc e
    ld a, [hli]
    ld [de], a
    inc e
    ld a, [hli]
    ld [de], a
    inc e
    dec b
    jr nz, .copy_shadow
    ret

; NES PPUCTRL bit 4 globally selects BG pattern table $0000/$1000.
; Our CGB representation stores that selection in each tile attribute's VRAM
; bank bit. Synchronize bit 3 deterministically across both maps whenever the
; NES global select changes. Do not XOR: a single stale/mismatched attribute
; would otherwise remain permanently opposite to the rest of the map.
nes_video_toggle_bg_pattern_bank:
    ld a, [nes_fit_screen]
    and a
    jr z, .toggle_normal
    ; Fit: never block the ISR on a full 240-tile recompose. Restart chunked work.
    ld a, $01
    ld [nes_fit_dirty], a
    xor a
    ld [nes_fit_recompose_my], a
    ret
.toggle_normal:

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
    ; Fit-screen identity maps own $9800/$9C00. SMB's stitched playfield would
    ; overwrite them with raw NES tile IDs and explode into garbage the moment
    ; horizontal scrolling / HUD split starts.
    ld a, [nes_fit_screen]
    and a
    jr nz, .disable

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

    ; Once a title has successfully established the vertical-mirroring
    ; horizontal stitch, remember that classification across temporary split
    ; loss during area/tileset transitions.
    ld a, $01
    ld [nes_hstitch_seen], a

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
    ld [nes_vram_unlocked], a
    ldh [rSVBK], a

    ; Rebuild this stitched column row-by-row from authoritative NES state.
    ; Tile ID and palette are published together. One VRAM wait covers both
    ; VBK writes for the row; unlocked mode skips waits while LY stays in VB.
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

    ; Same accessibility window as the tile-id write above.
    ld a, $01
    ldh [rVBK], a
    ld a, c
    ld [de], a
    pop bc

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

    xor a
    ld [nes_vram_unlocked], a
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
    ld a, [nes_fit_screen]
    and a
    jp nz, nes_video_fit_apply_scroll

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

    ; Fit-screen: keep 8x8 OBJ; NES 8x16 is flattened in OAM projection.
    ; Also pin LCDC.3 clear — identity publish owns $9800 only; toggling the
    ; BG map select showed the twin $9C00 surface that publish does not keep
    ; live (DK/Balloon Fight sprite/HUD mush under FIT_SCREEN).
    ld a, [nes_fit_screen]
    and a
    jr nz, .fit_store

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

.fit_store:
.store:
    ld a, b
.write:
    ldh [rLCDC], a
    ret

; ---------------------------------------------------------------------------
; Fit-screen identity metatiles: pack 2x2 half-CHR crumbs into tile 1+my*16+mx
; ---------------------------------------------------------------------------
; No runtime atlas/cache. VRAM bank 0 holds up to 240 composed BG tiles for the
; resident physical nametable page; bank 1 holds the active sprite PT.
; Scroll is NES/2 with letterbox. Stitch/follow stay off under fit.

nes_video_fit_upload_sprite_chr:
    ld a, $01
    ldh [rVBK], a
    ld a, [nes_ppuctrl]
    and $08
    ld [nes_fit_sprite_pt], a
    jr nz, .pt1
    ld hl, $4000
    jr .copy
.pt1:
    ld hl, $5000
.copy:
    ld de, $8000
    ld bc, $1000
    call nes_video_copy
    xor a
    ldh [rVBK], a
    ret

; Blank tile 0, identity maps on $9800/$9C00, clear resident page.
nes_video_fit_init_identity:
    xor a
    ldh [rVBK], a
    ld hl, $8000
    ld b, 16
.clear0:
    ld [hli], a
    dec b
    jr nz, .clear0

    call nes_video_fit_write_identity_map_9800
    call nes_video_fit_write_identity_map_9c00

    xor a
    ld [nes_fit_vram_page], a
    ld [nes_fit_dirty], a
    ld [nes_fit_recompose_my], a
    ld [nes_fit_origin_mx], a
    xor a
    sub 16
    ld [nes_fit_play_scx], a
    ldh [rSCX], a
    xor a
    sub 12
    ldh [rSCY], a
    ret

nes_video_fit_write_identity_map_9800:
    xor a
    ldh [rVBK], a
    ld de, $9800
    jr nes_video_fit_write_identity_map_de

nes_video_fit_write_identity_map_9c00:
    xor a
    ldh [rVBK], a
    ld de, $9C00

nes_video_fit_write_identity_map_de:
    ; Fill all 32 columns. Slot s (=c&15) is identity tile 1+my*16+s; columns
    ; 16-31 mirror 0-15 so SCX letterbox/wrap never walks into blank tile 0
    ; (smb-fit.mvl: map R half stayed empty while playfield SCX grew).
    ld b, 0
.row:
    ld c, 0
.col:
    ld a, b
    cp 15
    jr nc, .zero
    ld a, c
    and $0F                   ; s = c & 15
    ld h, a                   ; temp in H (HL free; DE is dest)
    ld a, b
    swap a
    and $F0
    add h
    inc a
    jr .store
.zero:
    xor a
.store:
    ld [de], a
    inc de
    inc c
    ld a, c
    cp 32
    jr c, .col
    inc b
    ld a, b
    cp 32
    jr c, .row

    ld a, d
    sub $04
    ld d, a
    ld a, $01
    ldh [rVBK], a
    ld bc, $0400
.attr:
    xor a
    ld [de], a
    inc de
    dec bc
    ld a, b
    or c
    jr nz, .attr
    xor a
    ldh [rVBK], a
    ret

nes_video_fit_apply_scroll:
    ; Fit pins LCDC.3 to $9800 (see nes_video_update_ctrl). Track the NES
    ; resident page for CHR compose only — never flip the GBC BG map select.
    ldh a, [rLCDC]
    and $F7
    ldh [rLCDC], a

    call nes_video_fit_displayed_page
    ld b, a
    ld a, [nes_fit_vram_page]
    cp b
    jr z, .maybe_flush
    ld a, b
    ld [nes_fit_vram_page], a
    ld a, $01
    ld [nes_fit_dirty], a
    xor a
    ld [nes_fit_recompose_my], a

.maybe_flush:
    ; One chunk per host frame max — never HBlank-pace all 240 tiles in one ISR.
    ld a, [nes_fit_dirty]
    and a
    jr z, .regs
    ld a, $01
    ld [nes_vram_unlocked], a
    call nes_video_fit_flush_dirty
    xor a
    ld [nes_vram_unlocked], a

.regs:
    call nes_video_fit_update_scroll_window
    xor a
    ldh [nes_seam_active], a
    ldh a, [rSTAT]
    and $BF
    ldh [rSTAT], a
    ret

; Half-scale letterbox scroll. Map cols 16-31 mirror 0-15 so SCX=240 letterbox
; never walks into blank tile 0. Sliding-origin windowing was rolled back:
; it broke single-screen DK / Balloon Fight while helping SMB mid-scroll.
nes_video_fit_update_scroll_window:
    xor a
    ld [nes_fit_origin_mx], a

    ; Always refresh play_scx for the fit STAT playfield half.
    ldh a, [nes_split_active]
    and a
    jr z, .scx_ppu
    ldh a, [nes_split_bottom_x]
    jr .scx_half
.scx_ppu:
    ld a, [nes_ppu_scroll_x]
.scx_half:
    srl a
    sub 16
    ld [nes_fit_play_scx], a

    ldh a, [nes_split_active]
    and a
    jr nz, .split_regs_done
    ; Single-screen: host owns SCX/SCY directly.
    ld a, [nes_fit_play_scx]
    ldh [rSCX], a
    ld a, [nes_ppu_scroll_y]
    srl a
    sub 12
    ldh [rSCY], a
.split_regs_done:
    ret

nes_video_fit_displayed_page:
    ld a, [nes_mirroring]
    cp $01
    jr z, .vertical
    ld a, [nes_ppuctrl]
    and $02
    add a
    ret
.vertical:
    ld a, [nes_ppuctrl]
    and $01
    add a
    add a
    ret

; If nes_fit_dirty set, compose as many identity rows as this VBlank allows.
; Full 240-tile HBlank-paced bursts were taking minutes of wall time at boot.
nes_video_fit_flush_dirty:
    ld a, [nes_fit_dirty]
    and a
    ret z
    ; fall through — dirty cleared only when the chunked pass finishes

; Recompose identity slots for nes_fit_vram_page from NT WRAM.
; LCD off: do the whole page. LCD on: one or more rows while LY>=144, then
; yield with dirty still set so the next host VBlank continues.
nes_video_fit_recompose_resident_page:
    ld a, [nes_fit_vram_page]
    ld [nes_fit_mt_page], a
    ld a, [nes_fit_recompose_my]
    cp 15
    jr c, .have_row
    xor a
.have_row:
    ld [nes_fit_mt_my], a

    ; LCD on: at most 2 rows per call (~32 metatiles) so boot cannot stall.
    ld b, 0
    ldh a, [rLCDC]
    bit 7, a
    jr z, .yloop
    ld b, 2

.yloop:
    call nes_video_fit_vblank_ok
    jr z, .yield
    ; LCD on: enforce per-call row budget. LCD off: b stays 0 and is ignored.
    ldh a, [rLCDC]
    bit 7, a
    jr z, .do_row
    ld a, b
    and a
    jr z, .yield
    dec b

.do_row:
    xor a
    ld [nes_fit_mt_mx], a
.xloop:
    ; Re-check every metatile so a long row cannot spill into active scanout
    ; via nes_video_wait_vram's HBlank fallback (mVL thrash signature).
    call nes_video_fit_vblank_ok
    jr z, .yield
    call nes_video_fit_publish_at_mx_my
    ; Publish returns Z when it deferred (VBlank ended). Do NOT advance mx —
    ; otherwise this metatile is skipped until a full dirty reset (holes/mush).
    jr z, .yield
    ld a, [nes_fit_mt_mx]
    inc a
    ld [nes_fit_mt_mx], a
    cp 16
    jr c, .xloop

    ld a, [nes_fit_mt_my]
    inc a
    ld [nes_fit_mt_my], a
    cp 15
    jr c, .yloop

    xor a
    ld [nes_fit_dirty], a
    ld a, 15
    ld [nes_fit_recompose_my], a
    ret

.yield:
    ; Stop before active scanout. mVL showed 80+ bank0 VRAM dumps/frame when
    ; compose continued via HBlank waits after LY wrapped past 144.
    ld a, [nes_fit_mt_my]
    ld [nes_fit_recompose_my], a
    ld a, $01
    ld [nes_fit_dirty], a
    ret

; NZ = safe to write fit BG CHR (LCD off or LY>=144). Z = active scanout.
nes_video_fit_vblank_ok:
    ldh a, [rLCDC]
    bit 7, a
    jr z, .ok
    ldh a, [rLY]
    cp 144
    jr c, .bad
.ok:
    or $01
    ret
.bad:
    xor a
    ret

nes_video_fit_sync_nametable_write:
    ld a, h
    and $03
    cp $03
    jr c, .fit_tile
    ld a, l
    cp $C0
    jp nc, nes_video_fit_sync_attribute_write

.fit_tile:
    ; Only the displayed physical page owns identity-slot CHR.
    ld a, h
    and $04
    ld b, a
    ld a, [nes_fit_vram_page]
    cp b
    ret nz

    ; Bulk flood already scheduled: skip per-tile work (finish via chunked flush).
    ld a, [nes_fit_dirty]
    and a
    ret nz

    ; Incremental publish only while unlocked AND still in VBlank/LCD-off.
    ; Unlocked queue drains that spill into mode-0 HBlank were rewriting bank0
    ; CHR on ~every scanline (smb-fit.mvl: ~59k active-line VRAM0 flushes).
    ld a, [nes_vram_unlocked]
    and a
    jr z, nes_video_fit_mark_dirty_if_resident
    call nes_video_fit_vblank_ok
    jr z, nes_video_fit_mark_dirty_if_resident

    ; Incremental: compose the 2x2 metatile containing this NT byte.
    ld a, l
    and $DE
    ld l, a
    jp nes_video_fit_publish_metatile_hl

nes_video_fit_mark_dirty_if_resident:
    ld a, h
    and $04
    ld b, a
    ld a, [nes_fit_vram_page]
    cp b
    ret nz
    ld a, [nes_fit_dirty]
    and a
    ret nz                    ; already chunking — do not restart from row 0
    ld a, $01
    ld [nes_fit_dirty], a
    xor a
    ld [nes_fit_recompose_my], a
    ret

; Kept for call sites that already have TL in HL (unused by dirty path).
nes_video_fit_publish_metatile_hl:
    ld a, $01
    ldh [rSVBK], a

    ld a, [hli]
    ld [nes_fit_mt_quad], a
    ld a, [hld]
    ld [nes_fit_mt_quad + 1], a
    ld a, l
    ld [nes_fit_mt_tmp_l], a
    ld a, h
    ld [nes_fit_mt_tmp_h], a
    ld a, l
    add $20
    ld l, a
    jr nc, .row2
    inc h
.row2:
    ld a, [hli]
    ld [nes_fit_mt_quad + 2], a
    ld a, [hl]
    ld [nes_fit_mt_quad + 3], a

    ld a, [nes_fit_mt_tmp_l]
    ld l, a
    ld a, [nes_fit_mt_tmp_h]
    ld h, a

    ld a, l
    and $1F
    srl a
    ld [nes_fit_mt_mx], a

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

    ld a, h
    and $04
    ld [nes_fit_mt_page], a
    jp nes_video_fit_publish_at_mx_my

; Compose + upload using nes_fit_mt_mx/my/page (resident-page local slots).
nes_video_fit_publish_at_mx_my:
    ; If quad not already filled (recompose path), load from NT.
    ld a, [nes_fit_mt_mx]
    add a
    ld c, a
    ld a, [nes_fit_mt_my]
    add a
    ld b, a
    ; Build NT addr for TL
    ld a, b
    and $07
    swap a
    add a
    or c
    ld l, a
    ld a, b
    srl a
    srl a
    srl a
    and $03
    ld d, a
    ld a, [nes_fit_mt_page]
    or d
    or $D0
    ld h, a

    ld a, $01
    ldh [rSVBK], a
    ld a, [hli]
    ld [nes_fit_mt_quad], a
    ld a, [hld]
    ld [nes_fit_mt_quad + 1], a
    ld a, l
    add $20
    ld l, a
    jr nc, .r2
    inc h
.r2:
    ld a, [hli]
    ld [nes_fit_mt_quad + 2], a
    ld a, [hl]
    ld [nes_fit_mt_quad + 3], a

    ; HL back to TL for palette
    ld a, b
    and $07
    swap a
    add a
    or c
    ld l, a
    ld a, b
    srl a
    srl a
    srl a
    and $03
    ld d, a
    ld a, [nes_fit_mt_page]
    or d
    or $D0
    ld h, a

    push hl
    call nes_video_fit_compose_quad
    pop hl

    push hl
    call nes_video_authoritative_tile_palette
    and $07
    ld [nes_fit_mt_tmp_h], a
    pop hl

    ; tile id = 1+my*16+mx
    ld a, [nes_fit_mt_my]
    swap a
    and $F0
    ld b, a
    ld a, [nes_fit_mt_mx]
    or b
    inc a
    ld [nes_fit_mt_tmp_l], a
    call nes_video_fit_upload_tile_a
    jr nz, .map_cells
    ; Compose burned the remainder of VBlank — defer; do not HBlank-pace.
    jp nes_video_fit_defer_dirty

.map_cells:
    ; Map cols (slot) and (slot|16) stay twins so SCX=240 letterbox never
    ; samples blank tile 0 in the high half (identity init mirrors both).
    call nes_video_fit_vblank_ok
    jp z, nes_video_fit_defer_dirty
    ld a, [nes_fit_mt_mx]
    and $0F
    call nes_video_fit_map_addr_a
    call nes_video_fit_write_map_cell_de
    ld a, e
    add 16
    ld e, a
    call nes_video_fit_write_map_cell_de
    or $01
    ret

; Mark dirty without resetting an in-progress chunk cursor.
nes_video_fit_defer_dirty:
    ld a, [nes_fit_dirty]
    and a
    jr nz, .keep
    ld a, $01
    ld [nes_fit_dirty], a
    xor a
    ld [nes_fit_recompose_my], a
.keep:
    xor a
    ret

; Write tile id (tmp_l) + attr (tmp_h) at DE. Assumes VBlank/LCD-off.
nes_video_fit_write_map_cell_de:
    xor a
    ldh [rVBK], a
    ld a, [nes_fit_mt_tmp_l]
    ld [de], a
    ld a, $01
    ldh [rVBK], a
    ld a, [nes_fit_mt_tmp_h]
    ld [de], a
    xor a
    ldh [rVBK], a
    ret

; A = map column 0..15. DE = $9800 + my*32 + A (fit pins LCDC.3 to map 0).
nes_video_fit_map_addr_a:
    and $0F
    ld c, a
    ld de, $9800
.base:
    ld a, [nes_fit_mt_my]
    ld l, a
    ld h, 0
    add hl, hl
    add hl, hl
    add hl, hl
    add hl, hl
    add hl, hl                 ; my*32
    ld a, c
    add l
    ld l, a
    jr nc, .ok
    inc h
.ok:
    add hl, de
    ld d, h
    ld e, l
    ret

nes_video_fit_compose_quad:
    ld hl, nes_fit_mt_compose
    ld b, 16
    xor a
.clear:
    ld [hli], a
    dec b
    jr nz, .clear

    ld a, [nes_chr_gbc_bank_base]
    ld b, a
    ld a, [nes_chr_bank]
    add b
    ld [$2000], a

    ld a, [nes_ppuctrl]
    and $10
    jr z, .pt0
    ld d, $50
    jr .base_ready
.pt0:
    ld d, $40
.base_ready:

    xor a
.quad_loop:
    cp 4
    jp nc, .compose_done
    ld [nes_fit_mt_tmp_l], a

    ld c, a
    ld hl, nes_fit_mt_quad
    ld a, l
    add c
    ld l, a
    jr nc, .q_ok
    inc h
.q_ok:
    ld a, [hl]
    ld l, a
    ld h, 0
    add hl, hl
    add hl, hl
    add hl, hl
    add hl, hl
    ld a, d
    add h
    ld h, a

    ld a, [nes_fit_mt_tmp_l]
    ld c, a
    and $02
    add a
    ld b, a
    ld a, c
    and $01
    ld c, a

    xor a
.row_loop:
    cp 4
    jr nc, .next_quad
    ld [nes_fit_mt_tmp_h], a

    push hl
    push de
    add a
    add l
    ld l, a
    jr nc, .src_ok
    inc h
.src_ok:
    ld a, [hli]
    and $F0
    ld e, a
    ld a, [hl]
    and $F0
    ld d, a

    ld a, [nes_fit_mt_tmp_h]
    add b
    add a
    ld l, a
    ld h, HIGH(nes_fit_mt_compose)
    ld a, LOW(nes_fit_mt_compose)
    add l
    ld l, a
    jr nc, .dst_ok
    inc h
.dst_ok:

    ld a, c
    and a
    jr nz, .right
    ld a, [hl]
    or e
    ld [hli], a
    ld a, [hl]
    or d
    ld [hl], a
    jr .row_done
.right:
    ld a, e
    swap a
    ld e, a
    ld a, d
    swap a
    ld d, a
    ld a, [hl]
    or e
    ld [hli], a
    ld a, [hl]
    or d
    ld [hl], a
.row_done:
    pop de
    pop hl
    ld a, [nes_fit_mt_tmp_h]
    inc a
    jr .row_loop

.next_quad:
    ld a, [nes_fit_mt_tmp_l]
    inc a
    jr .quad_loop

.compose_done:
    jp nes_restore_code_bank

nes_video_fit_upload_tile_a:
    ; BG CHR upload is VBlank/LCD-off only. nes_video_wait_vram's HBlank
    ; fallback was still rewriting bank0 on ~every scanline after unlock
    ; cleared (smb-fit-a/b: 25k–52k active-line VRAM0 flushes).
    call nes_video_fit_vblank_ok
    ret z
    ld a, [nes_fit_mt_tmp_l]
    ld l, a
    ld h, 0
    add hl, hl
    add hl, hl
    add hl, hl
    add hl, hl
    ld a, h
    or $80
    ld d, a
    ld e, l
    xor a
    ldh [rVBK], a
    ld hl, nes_fit_mt_compose
    ld b, 16
.copy:
    ; VBlank/LCD-off: VRAM is freely writable — do not STAT-poll mode 0/3.
    ld a, [hli]
    ld [de], a
    inc de
    dec b
    jr nz, .copy
    or $01                    ; NZ = success (dec b left Z set)
    ret

nes_video_fit_sync_attribute_write:
    ; Tile publishes already sample authoritative attrs. While unlocked
    ; (incremental column flush), skip — avoids restarting a full-page chunk
    ; on every SMB attribute touch. Deferred/locked paths still coalesce.
    ld a, [nes_vram_unlocked]
    and a
    ret nz
    jp nes_video_fit_mark_dirty_if_resident

nes_video_fit_sync_sprite_chr:
    ld a, [nes_fit_screen]
    and a
    ret z
    ld a, [nes_ppuctrl]
    and $08
    ld b, a
    ld a, [nes_fit_sprite_pt]
    cp b
    ret z

    ld a, [nes_vram_unlocked]
    ld c, a
    ld a, $01
    ld [nes_vram_unlocked], a
    push bc

    ld a, [nes_chr_gbc_bank_base]
    ld b, a
    ld a, [nes_chr_bank]
    add b
    ld [$2000], a
    call nes_video_fit_upload_sprite_chr
    call nes_restore_code_bank

    pop bc
    ld a, c
    ld [nes_vram_unlocked], a
    ret
