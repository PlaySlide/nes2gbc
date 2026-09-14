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

    ; LCD on, BG on, map $9800. Fit uses signed BG tile IDs so the
    ; $8800-$97FF BG region can coexist with sprites in $8000-$87FF.
    ld a, [nes_fit_screen]
    and a
    ld a, $91
    jr z, .init_lcdc_store
    ld a, $81
.init_lcdc_store:
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
    ; SMB-style vertical-mirroring raster scroll deliberately constructs future
    ; columns offscreen. Do not classify those parser transactions as a screen-
    ; wide flood; exact visibility filtering below will publish only live cells.
    ld a, [nes_mirroring]
    cp $01
    jr nz, .fit_bulk_size
    ldh a, [nes_split_active]
    and a
    jr nz, .fit_bulk_done
.fit_bulk_size:
    ld a, [nes_nametable_queue_ptr_hi]
    cp $D8
    jr nz, .fit_bulk
    ld a, [nes_nametable_queue_ptr_lo]
    cp 96                      ; >= 48 tile entries (2 bytes each)
    jr c, .fit_bulk_done
.fit_bulk:
    ; Coalesce with an in-progress chunk. Resetting recompose_my on every large
    ; flood (Balloon Fight title/Game A) starved the LCD-on row budget and made
    ; fit feel far slower than 1x. Fresh dirty still starts at row 0.
    ld a, [nes_fit_dirty]
    and a
    jr nz, .fit_bulk_keep
    ld a, $01
    ld [nes_fit_dirty], a
    xor a
    ld [nes_fit_recompose_my], a
    jr .fit_bulk_done
.fit_bulk_keep:
    ld a, $01
    ld [nes_fit_dirty], a
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
    ; 160x120 fit keeps the proven half-height vertical mapping.
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
    ; 160px-wide fit: X = floor(NES_X * 5/8), no side letterbox.
    push hl
    ldh a, [nes_view_coord_tmp]
    ld c, a
    ld b, $00
    call nes_video_fit_scale_x_bc
    ld a, l
    pop hl
    cp $A0
    jp nc, .next_source
    add $08
    ldh [nes_oam_proj_x_tmp], a

.x_ready:
    ; A 5x4 crumb lives in the top-left of an 8x8 OBJ tile. Hardware flips the
    ; whole 8x8 box, so compensate the anchor to keep the visible crumb fixed.
    ld a, [nes_fit_screen]
    and a
    jr z, .fit_flip_done
    ldh a, [nes_sprite_attr_tmp]
    bit 7, a
    jr z, .fit_no_vflip_adjust
    ldh a, [nes_oam_proj_y_tmp]
    sub 4
    ldh [nes_oam_proj_y_tmp], a
.fit_no_vflip_adjust:
    ldh a, [nes_sprite_attr_tmp]
    bit 6, a
    jr z, .fit_flip_done
    ldh a, [nes_oam_proj_x_tmp]
    sub 3
    ldh [nes_oam_proj_x_tmp], a
.fit_flip_done:

    ; Visible sprite: pack it into the next CGB OAM slot.
    ldh a, [nes_oam_proj_y_tmp]
    ld [de], a
    inc de
    ldh a, [nes_oam_proj_x_tmp]
    ld [de], a
    inc de

    ; Tile number and pattern-table bank. Fit stores the active 256-tile
    ; sprite PT across the non-BG lower half of both VRAM banks: source 0-127
    ; -> bank0 tile 0-127, source 128-255 -> bank1 tile 0-127.
    ld a, [nes_fit_screen]
    and a
    jr z, .normal_sprite_tile
    ldh a, [nes_view_sprite_tile_tmp]
    ld c, a
    and $7F
    ld [de], a
    inc de
    bit 7, c
    ld a, $00
    jr z, .bank_ready
    ld a, $08
    jr .bank_ready

.normal_sprite_tile:
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
    ; Signed BG tile addressing exposes $8800-$97FF while leaving $8000-$87FF
    ; free for the split sprite pattern table in both VRAM banks.
    ld a, b
    and $EF
    jr .write
.store:
    ld a, b
.write:
    ldh [rLCDC], a
    ret

; ---------------------------------------------------------------------------
; Fit-screen 160x120 4:3 scaler
; ---------------------------------------------------------------------------
; NES 256x240 -> GBC 160x120: X = 5/8, Y = 1/2. This matches the NES's
; intended 4:3 display aspect while using the full GBC width and retaining only
; 12px letterbox bars above/below. Each NES 8x8 CHR tile is preconverted to a
; 5x4 crumb. Two NES tile rows therefore align exactly with one 8px GBC row.
;
; BG uses signed tile addressing ($8800-$97FF). Each physical $9800 map
; column owns one pattern slot per row: 32x15 = 480 identity slots, split across
; VRAM bank0 (slots 0-255) and bank1 (slots 256-479). Only 21 consecutive columns
; are live at once (20 full tiles plus the fine-scroll edge). The lower
; $8000-$87FF half of both banks remains independent sprite storage; the active
; 256-tile NES sprite PT is split 128/128 across those two banks.

nes_video_fit_upload_sprite_chr:
    ld a, [nes_ppuctrl]
    and $08
    ld [nes_fit_sprite_pt], a
    jr nz, .pt1
    ld hl, $4000
    jr .copy
.pt1:
    ld hl, $5000
.copy:
    xor a
    ldh [rVBK], a
    ld de, $8000
    ld bc, $0800
    call nes_video_copy
    ld a, $01
    ldh [rVBK], a
    ld de, $8000
    ld bc, $0800
    call nes_video_copy
    xor a
    ldh [rVBK], a
    ret

; Clear signed BG tile storage and both maps. Blank map cells use spare bank1
; signed tile $60 (local slot 224, immediately after the 480 physical BG slots).
nes_video_fit_init_identity:
    xor a
    ldh [rVBK], a
    ld hl, $8800
    ld bc, $1000
    call nes_video_fill_zero
    ld a, $01
    ldh [rVBK], a
    ld hl, $8800
    ld bc, $1000
    call nes_video_fill_zero

    xor a
    ldh [rVBK], a
    ld hl, $9800
    ld bc, $0800
    ld d, $60
.map_blank:
    ld a, d
    ld [hli], a
    dec bc
    ld a, b
    or c
    jr nz, .map_blank

    ld a, $01
    ldh [rVBK], a
    ld hl, $9800
    ld bc, $0800
    ld d, $08
.attr_blank:
    ld a, d
    ld [hli], a
    dec bc
    ld a, b
    or c
    jr nz, .attr_blank

    xor a
    ldh [rVBK], a
    ld [nes_fit_vram_page], a
    ld [nes_fit_dirty], a
    ld [nes_fit_recompose_my], a
    ld [nes_fit_mt_mx], a
    ld [nes_fit_origin_mx], a
    ld [nes_fit_play_scx], a
    ldh [rSCX], a
    sub 12
    ldh [rSCY], a
    ret

nes_video_fit_apply_scroll:
    ; Fit always owns $9800 and signed BG addressing.
    ldh a, [rLCDC]
    and $E7
    ldh [rLCDC], a

    call nes_video_fit_displayed_page
    ld b, a
    ld a, [nes_fit_vram_page]
    and $04
    cp b
    jr z, .page_done
    ld a, [nes_fit_vram_page]
    and $F8
    or b
    ld [nes_fit_vram_page], a
    ld a, $01
    ld [nes_fit_dirty], a
    xor a
    ld [nes_fit_recompose_my], a
    ld [nes_fit_mt_mx], a
.page_done:
    call nes_video_fit_update_scroll_window

    ld a, [nes_fit_dirty]
    and a
    jr z, .regs_done
    ld a, $01
    ld [nes_vram_unlocked], a
    call nes_video_fit_flush_dirty
    xor a
    ld [nes_vram_unlocked], a

.regs_done:
    xor a
    ldh [nes_seam_active], a
    ldh a, [rSTAT]
    and $BF
    ldh [rSTAT], a
    ret

; BC = NES X (0..511). Return HL=floor(BC*5/8) (0..319).
nes_video_fit_scale_x_bc:
    ld h, b
    ld l, c
    add hl, hl
    add hl, hl
    add hl, bc
    srl h
    rr l
    srl h
    rr l
    srl h
    rr l
    ret

; 21-column circular live window over the 40 GBC tile columns that represent
; the two-nametable horizontal NES world at 5/8 scale. A 160px viewport needs
; 20 full tiles plus one partial entering tile whenever SCX has a fine offset.
nes_video_fit_update_scroll_window:
    ld a, [nes_mirroring]
    cp $01
    jr nz, .resident
    ldh a, [nes_split_active]
    and a
    jr z, .resident
    jr .slide_vert

.resident:
    ld a, [nes_fit_origin_mx]
    and a
    jr nz, .resident_reset
    ld a, [nes_fit_vram_page]
    and $F8
    jr z, .resident_origin_ok
.resident_reset:
    xor a
    ld [nes_fit_origin_mx], a
    ld a, [nes_fit_vram_page]
    and $04
    ld [nes_fit_vram_page], a
    ld a, $01
    ld [nes_fit_dirty], a
    xor a
    ld [nes_fit_recompose_my], a
    ld [nes_fit_mt_mx], a
.resident_origin_ok:
    ldh a, [nes_split_active]
    and a
    jr z, .resident_scroll
    ldh a, [nes_split_bottom_x]
    jr .resident_scale
.resident_scroll:
    ld a, [nes_ppu_scroll_x]
.resident_scale:
    ld c, a
    ld b, $00
    call nes_video_fit_scale_x_bc
    ld a, l
    ld [nes_fit_play_scx], a
    jp .apply_host_regs

.slide_vert:
    ; Effective NES X in BC (0..511), including logical nametable bit 0.
    ldh a, [nes_split_bottom_x]
    ld c, a
    ldh a, [nes_view_x]
    add c
    ld c, a
    ld b, $00
    jr nc, .eff_nt
    inc b
.eff_nt:
    ldh a, [nes_split_bottom_ctrl]
    and $01
    xor b
    ld b, a

    call nes_video_fit_scale_x_bc
    ld a, l
    and $07
    ld e, a                    ; fine host pixels
    srl h
    rr l
    srl h
    rr l
    srl h
    rr l
    ld c, l                    ; host world tile origin 0..39

    ld a, [nes_fit_origin_mx]
    cp c
    jp z, .scx_from_ring
    ld b, a                    ; old world origin 0..39
    ld a, c
    ld [nes_fit_origin_mx], a

    ; If a previous entering-column update is still unfinished, fall back to a
    ; coherent 21-column rebuild at the new origin rather than exposing a slot
    ; whose ownership is ambiguous. SMB parser floods no longer create this
    ; state in steady scrolling, so this is now an exceptional catch-up path.
    ld a, [nes_fit_dirty]
    and a
    jp nz, .full_dirty_rebase

    ; Treat the 39->0/0->39 wrap as an ordinary one-column move in the 40-column
    ; scaled NES world.
    ld a, b
    cp 39
    jr nz, .check_wrap_minus
    ld a, c
    and a
    jp z, .delta_plus1
.check_wrap_minus:
    ld a, b
    and a
    jr nz, .delta_regular
    ld a, c
    cp 39
    jp z, .delta_minus1
.delta_regular:
    ld a, c
    sub b
    cp 1
    jp z, .delta_plus1
    cp $FF
    jp z, .delta_minus1
    jp .full_dirty_rebase

.delta_plus1:
    ; Advance the physical GBC map ring one column. Bits 3-7 hold ring head;
    ; bit 2 retains the resident NES physical-page selector.
    ld a, [nes_fit_vram_page]
    ld d, a
    and $F8
    add $08
    and $F8
    ld b, a
    ld a, d
    and $04
    or b
    ld [nes_fit_vram_page], a
    ld a, 2
    ld [nes_fit_dirty], a
    xor a
    ld [nes_fit_recompose_my], a
    ld a, 20                   ; entering partial/right-edge column
    ld [nes_fit_mt_mx], a
    jp .scx_from_ring

.delta_minus1:
    ld a, [nes_fit_vram_page]
    ld d, a
    and $F8
    sub $08
    and $F8
    ld b, a
    ld a, d
    and $04
    or b
    ld [nes_fit_vram_page], a
    ld a, 3
    ld [nes_fit_dirty], a
    xor a
    ld [nes_fit_recompose_my], a
    ld [nes_fit_mt_mx], a
    jp .scx_from_ring

.full_dirty_rebase:
    ; A discontinuity gets a fresh physical ring head. With a full rebuild in
    ; progress, rebasing keeps SCX and map ownership coherent.
    ld a, c
    and $1F
    add a
    add a
    add a
    ld b, a
    ld a, [nes_fit_vram_page]
    and $04
    or b
    ld [nes_fit_vram_page], a
    ld a, $01
    ld [nes_fit_dirty], a
    xor a
    ld [nes_fit_recompose_my], a
    ld [nes_fit_mt_mx], a

.scx_from_ring:
    ; Hardware SCX follows the physical map ring, not the modulo-40 world
    ; column. This is what lets world columns 32..39 and wrapped 0.. coexist.
    ld a, [nes_fit_vram_page]
    and $F8
    add e
    ld [nes_fit_play_scx], a

.apply_host_regs:
    ldh a, [nes_split_active]
    and a
    jr nz, .split_regs_done
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

nes_video_fit_flush_dirty:
    ld a, [nes_fit_dirty]
    and a
    ret z

nes_video_fit_recompose_resident_page:
    ld a, [nes_fit_recompose_my]
    cp 15
    jr c, .have_row
    xor a
.have_row:
    ld [nes_fit_mt_my], a

    ; Full rebuild covers the 21 potentially scanned columns. Entering columns
    ; get four tiles per call so ordinary horizontal scroll can catch up.
    ld b, 0
    ldh a, [rLCDC]
    bit 7, a
    jr z, .mode
    ld b, 1
.mode:
    ld a, [nes_fit_dirty]
    cp 2
    jr z, .col_right
    cp 3
    jr z, .col_left
    jr .yloop

.col_right:
    ld a, 20
    jr .col_set
.col_left:
    xor a
.col_set:
    ld [nes_fit_mt_mx], a
    ldh a, [rLCDC]
    bit 7, a
    jr z, .col_yloop
    ld b, 4
.col_yloop:
    call nes_video_fit_vblank_ok
    jr z, .yield_col
    ldh a, [rLCDC]
    bit 7, a
    jr z, .col_do
    ld a, b
    and a
    jr z, .yield_col
    dec b
.col_do:
    push bc
    call nes_video_fit_publish_at_mx_my
    pop bc
    jr z, .yield_col
    ld a, [nes_fit_mt_my]
    inc a
    ld [nes_fit_mt_my], a
    cp 15
    jr c, .col_yloop
    xor a
    ld [nes_fit_dirty], a
    ld a, 15
    ld [nes_fit_recompose_my], a
    ret
.yield_col:
    ld a, [nes_fit_mt_my]
    ld [nes_fit_recompose_my], a
    ret

.yloop:
    call nes_video_fit_vblank_ok
    jr z, .yield
    ldh a, [rLCDC]
    bit 7, a
    jr z, .do_row
    ld a, b
    and a
    jr z, .yield
    dec b
.do_row:
.xloop:
    call nes_video_fit_vblank_ok
    jr z, .yield
    push bc
    call nes_video_fit_publish_at_mx_my
    pop bc
    jr z, .yield
    ld a, [nes_fit_mt_mx]
    inc a
    ld [nes_fit_mt_mx], a
    cp 21
    jr c, .xloop

    xor a
    ld [nes_fit_mt_mx], a
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
    ld a, [nes_fit_mt_my]
    ld [nes_fit_recompose_my], a
    ld a, $01
    ld [nes_fit_dirty], a
    ret

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
    ld a, [nes_fit_dirty]
    and a
    ret nz
    ld a, [nes_vram_unlocked]
    and a
    jr z, nes_video_fit_mark_dirty_if_resident
    ld a, [nes_mirroring]
    cp $01
    jr nz, .fit_tile_vblank
    ldh a, [nes_split_active]
    and a
    jp nz, nes_video_fit_publish_source_tile_hl
.fit_tile_vblank:
    call nes_video_fit_vblank_ok
    jr z, nes_video_fit_mark_dirty_if_resident
    jp nes_video_fit_publish_source_tile_hl

nes_video_fit_mark_dirty_if_resident:
    ld a, [nes_fit_dirty]
    and a
    ret nz
    ld a, $01
    ld [nes_fit_dirty], a
    xor a
    ld [nes_fit_recompose_my], a
    ld [nes_fit_mt_mx], a
    ret

; Compatibility gate retained for older callers; the wide publisher performs
; exact visibility checks in scaled world coordinates.
nes_video_fit_hl_in_fit_window:
    xor a
    ret

nes_video_fit_publish_metatile_hl:
    jp nes_video_fit_publish_source_tile_hl

; Publish every destination tile touched by one changed NES 8x8 source tile.
; X scaling maps one source tile to five host pixels, so it can overlap at most
; two 8px destination tiles. Y/2 maps each NES tile row into exactly one host row.
nes_video_fit_publish_source_tile_hl:
    ; Source NES tile row -> fit row.
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

    ; Source world tile X. Under vertical mirroring physical page 1 is the
    ; horizontal neighbor; otherwise only the resident physical page matters.
    ld a, l
    and $1F
    ld c, a
    ld a, [nes_mirroring]
    cp $01
    jr z, .src_vertical
    ld a, h
    and $04
    ld b, a
    ld a, [nes_fit_vram_page]
    and $04
    cp b
    ret nz
    jr .src_x_ready
.src_vertical:
    ld a, h
    and $04
    jr z, .src_x_ready
    ld a, c
    or $20
    ld c, a
.src_x_ready:
    ; host_x = source_tile_x*5, then dest_world_col=host_x/8, rem=host_x&7.
    ld d, $00
    ld e, c
    ld h, d
    ld l, e
    add hl, hl
    add hl, hl
    add hl, de
    ld a, l
    and $07
    ld e, a                    ; rem
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
    ld d, a                    ; first world destination column

    ; Precompute optional second viewport offset in quad+3 ($FF = none).
    ld a, $FF
    ld [nes_fit_mt_quad + 3], a
    ld a, e
    cp 4
    jr c, .second_done
    ld a, d
    inc a
    cp 40
    jr c, .second_world_ready
    sub 40
.second_world_ready:
    ld b, a
    ld a, [nes_fit_origin_mx]
    ld c, a
    ld a, b
    sub c
    jr nc, .second_delta_ready
    add 40
.second_delta_ready:
    cp 21
    jr nc, .second_done
    ld [nes_fit_mt_quad + 3], a
.second_done:

    ; First destination if visible in [origin, origin+21).
    ld a, [nes_fit_origin_mx]
    ld c, a
    ld a, d
    sub c
    jr nc, .first_delta_ready
    add 40
.first_delta_ready:
    cp 21
    jr nc, .after_first
    ld [nes_fit_mt_mx], a
    call nes_video_fit_publish_at_mx_my
.after_first:
    ld a, [nes_fit_mt_quad + 3]
    cp $FF
    ret z
    ld [nes_fit_mt_mx], a
    jp nes_video_fit_publish_at_mx_my

; A = current destination world column (0..39).
nes_video_fit_world_col:
    ld a, [nes_fit_origin_mx]
    ld b, a
    ld a, [nes_fit_mt_mx]
    add b
    cp 40
    ret c
    sub 40
    ret

; Table: world GBC column -> first 5px NES-tile column, phase inside that crumb.
nes_video_fit_wide_x_table:
    db 0,0, 1,3, 3,1, 4,4, 6,2, 8,0, 9,3, 11,1, 12,4, 14,2
    db 16,0, 17,3, 19,1, 20,4, 22,2, 24,0, 25,3, 27,1, 28,4, 30,2
    db 32,0, 33,3, 35,1, 36,4, 38,2, 40,0, 41,3, 43,1, 44,4, 46,2
    db 48,0, 49,3, 51,1, 52,4, 54,2, 56,0, 57,3, 59,1, 60,4, 62,2

; Compose + upload one 8x8 destination tile at viewport offset mx,my.
nes_video_fit_publish_at_mx_my:
    call nes_video_fit_compose_wide

    ; Palette from the center source tile (the dominant attribute region for
    ; most mixed 10px NES attribute boundaries).
    call nes_video_fit_world_col
    add a
    ld e, a
    ld d, $00
    ld hl, nes_video_fit_wide_x_table
    add hl, de
    ld a, [hli]
    ld b, a                    ; q
    ld a, [hl]                 ; phase
    and a
    jr z, .center_x_ready
    inc b
.center_x_ready:
    ld a, b
    and $3F
    ld b, a
    ld a, [nes_fit_mt_my]
    add a
    inc a
    ld [nes_fit_mt_tmp_h], a
    ld a, b
    call nes_video_fit_read_nt_tile_a
    push hl
    call nes_video_authoritative_tile_palette
    and $07
    ld c, a
    pop hl

    ; Physical map column = ring_head + viewport offset (mod 32).
    ld a, [nes_fit_vram_page]
    and $F8
    srl a
    srl a
    srl a
    ld b, a
    ld a, [nes_fit_mt_mx]
    add b
    and $1F
    ld [nes_fit_mt_page], a

    ; Pattern slot = my*32 + physical_map_col (0..479). H=0/1 chooses
    ; BG VRAM bank; L becomes the signed local tile ID after adding $80.
    ld a, [nes_fit_mt_my]
    ld l, a
    ld h, $00
    add hl, hl
    add hl, hl
    add hl, hl
    add hl, hl
    add hl, hl
    ld a, [nes_fit_mt_page]
    ld e, a
    ld d, $00
    add hl, de
    ld a, l
    add $80
    ld [nes_fit_mt_tmp_l], a   ; signed BG tile number

    ld a, c
    ld b, a
    ld a, h
    and a
    ld a, b
    jr z, .slot_attr_ready
    or $08
.slot_attr_ready:
    ld [nes_fit_mt_tmp_h], a

    call nes_video_fit_upload_tile_a
    jr nz, .map_cell
    jp nes_video_fit_defer_dirty

.map_cell:
    call nes_video_fit_vblank_ok
    jp z, nes_video_fit_defer_dirty
    ld a, [nes_fit_mt_page]
    call nes_video_fit_map_addr_a
    call nes_video_fit_write_map_cell_de
    or $01
    ret

nes_video_fit_defer_dirty:
    ld a, [nes_fit_dirty]
    and a
    jr nz, .keep
    ld a, $01
    ld [nes_fit_dirty], a
    xor a
    ld [nes_fit_recompose_my], a
    ld [nes_fit_mt_mx], a
.keep:
    xor a
    ret

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

; A = map column 0..31. DE = $9800 + my*32 + A.
nes_video_fit_map_addr_a:
    and $1F
    ld c, a
    ld de, $9800
    ld a, [nes_fit_mt_my]
    ld l, a
    ld h, $00
    add hl, hl
    add hl, hl
    add hl, hl
    add hl, hl
    add hl, hl
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

; Read one authoritative NES nametable tile. A=world NES tile X 0..63;
; source NES tile row is nes_fit_mt_tmp_h. Returns A=tile ID and HL=NT address.
nes_video_fit_read_nt_tile_a:
    and $3F
    ld c, a
    ld a, [nes_mirroring]
    cp $01
    jr z, .vertical
    ld a, c
    and $1F
    ld c, a
    ld a, [nes_fit_vram_page]
    and $04
    ld d, a
    jr .addr
.vertical:
    ld a, c
    and $20
    jr z, .p0
    ld d, $04
    jr .local_x
.p0:
    ld d, $00
.local_x:
    ld a, c
    and $1F
    ld c, a
.addr:
    ld a, [nes_fit_mt_tmp_h]
    ld b, a
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
    or d
    or $D0
    ld h, a
    ld a, $01
    ldh [rSVBK], a
    ld a, [hl]
    ret

; A=q first NES tile column, B=NES tile row. Load three source tile IDs.
nes_video_fit_load_triplet:
    ld [nes_fit_mt_page], a
    ld a, b
    ld [nes_fit_mt_tmp_h], a
    ld a, [nes_fit_mt_page]
    call nes_video_fit_read_nt_tile_a
    ld [nes_fit_mt_quad], a
    ld a, [nes_fit_mt_page]
    inc a
    and $3F
    ld [nes_fit_mt_page], a
    call nes_video_fit_read_nt_tile_a
    ld [nes_fit_mt_quad + 1], a
    ld a, [nes_fit_mt_page]
    inc a
    and $3F
    call nes_video_fit_read_nt_tile_a
    ld [nes_fit_mt_quad + 2], a
    ret

; Input A=converted tile ID, C=crumb row 0..3. Output D=lo,E=hi row bytes.
nes_video_fit_get_scaled_row_a_c:
    ld l, a
    ld h, $00
    add hl, hl
    add hl, hl
    add hl, hl
    add hl, hl
    ld a, c
    add a
    add l
    ld l, a
    jr nc, .row_addr_ok
    inc h
.row_addr_ok:
    ld a, [nes_ppuctrl]
    and $10
    ld a, $40
    jr z, .base
    ld a, $50
.base:
    add h
    ld h, a
    ld a, [hli]
    ld d, a
    ld a, [hl]
    ld e, a
    ret

; B/C/D = three left-aligned 5-bit chunks, phase in nes_fit_mt_tmp_l.
; Return packed 8-pixel row in A.
nes_video_fit_pack_plane:
    ld a, [nes_fit_mt_tmp_l]
    and a
    jr z, .p0
    cp 1
    jr z, .p1
    cp 2
    jr z, .p2
    cp 3
    jr z, .p3
    ; p4: 1 from A, 5 from B, 2 from C.
    ld a, b
    swap a
    and $80
    ld e, a
    ld a, c
    srl a
    and $7C
    or e
    ld e, a
    ld a, d
    swap a
    srl a
    srl a
    and $03
    or e
    ret
.p0:
    ld a, b
    and $F8
    ld e, a
    ld a, c
    srl a
    srl a
    srl a
    srl a
    srl a
    or e
    ret
.p1:
    ld a, b
    add a
    and $F0
    ld e, a
    ld a, c
    swap a
    and $0F
    or e
    ret
.p2:
    ld a, b
    add a
    add a
    and $E0
    ld e, a
    ld a, c
    srl a
    srl a
    srl a
    and $1F
    or e
    ret
.p3:
    ld a, b
    add a
    add a
    add a
    and $C0
    ld e, a
    ld a, c
    srl a
    srl a
    and $3E
    or e
    ld e, a
    ld a, d
    rlca
    and $01
    or e
    ret

; Tile IDs in quad0..2, phase in tmp_l. A=destination row base (0 or 4).
nes_video_fit_compose_half:
    ld [nes_fit_mt_tmp_h], a
    xor a
    ld [nes_fit_mt_page], a
.row:
    ; Fetch all three source crumbs and use the native stack for temporary
    ; plane bytes. This avoids overlapping bottom output rows 5/6.
    ld a, [nes_fit_mt_page]
    ld c, a
    ld a, [nes_fit_mt_quad]
    call nes_video_fit_get_scaled_row_a_c
    push de
    ld a, [nes_fit_mt_page]
    ld c, a
    ld a, [nes_fit_mt_quad + 1]
    call nes_video_fit_get_scaled_row_a_c
    push de
    ld a, [nes_fit_mt_page]
    ld c, a
    ld a, [nes_fit_mt_quad + 2]
    call nes_video_fit_get_scaled_row_a_c
    push de

    pop hl
    pop de
    pop bc
    ; BC = tile0 lo/hi, DE = tile1 lo/hi, HL = tile2 lo/hi.
    ld a, c
    push af
    ld a, e
    push af
    ld a, l
    push af
    ld c, d
    ld d, h
    call nes_video_fit_pack_plane
    ld h, a
    pop af
    ld d, a
    pop af
    ld c, a
    pop af
    ld b, a
    call nes_video_fit_pack_plane
    ld d, h
    ld e, a

    ld a, [nes_fit_mt_tmp_h]
    ld b, a
    ld a, [nes_fit_mt_page]
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
    ld a, d
    ld [hli], a
    ld a, e
    ld [hl], a

    ld a, [nes_fit_mt_page]
    inc a
    ld [nes_fit_mt_page], a
    cp 4
    jr c, .row
    ret

nes_video_fit_compose_wide:
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

    ; Locate horizontal source q/phase.
    call nes_video_fit_world_col
    add a
    ld e, a
    ld d, $00
    ld hl, nes_video_fit_wide_x_table
    add hl, de
    ld a, [hli]
    ld b, a
    ld a, [hl]
    ld [nes_fit_mt_tmp_l], a
    ld a, [nes_fit_mt_my]
    add a
    ld c, a
    ld a, b
    ld b, c
    call nes_video_fit_load_triplet
    xor a
    call nes_video_fit_compose_half

    ; Bottom NES tile row; recompute q because compose_half owns scratch state.
    call nes_video_fit_world_col
    add a
    ld e, a
    ld d, $00
    ld hl, nes_video_fit_wide_x_table
    add hl, de
    ld a, [hli]
    ld b, a
    ld a, [hl]
    ld [nes_fit_mt_tmp_l], a
    ld a, [nes_fit_mt_my]
    add a
    inc a
    ld c, a
    ld a, b
    ld b, c
    call nes_video_fit_load_triplet
    ld a, 4
    call nes_video_fit_compose_half

    jp nes_restore_code_bank

nes_video_fit_upload_tile_a:
    call nes_video_fit_vblank_ok
    ret z

    ; Signed tile number -> physical local slot: id $80 maps to $8800, id $00
    ; maps to $9000. tmp_h bit3 selects VRAM bank for slots 256-299.
    ld a, [nes_fit_mt_tmp_l]
    sub $80
    ld l, a
    ld h, $00
    add hl, hl
    add hl, hl
    add hl, hl
    add hl, hl
    ld a, h
    add $88
    ld h, a
    ld d, h
    ld e, l

    ld a, [nes_fit_mt_tmp_h]
    and $08
    jr z, .bank0
    ld a, $01
    jr .bank_store
.bank0:
    xor a
.bank_store:
    ldh [rVBK], a

    ld hl, nes_fit_mt_compose
    ld b, 16
.copy:
    ld a, [hli]
    ld [de], a
    inc de
    dec b
    jr nz, .copy
    xor a
    ldh [rVBK], a
    ld a, $01
    and a
    ret

nes_video_fit_sync_attribute_write:
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
