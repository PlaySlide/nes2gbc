; NES PPU register semantics backed by GBC WRAM/ROM banks.
; This is a semantic model, not cycle-accurate PPU emulation.

SECTION "NES PPU helpers", ROM0

; Input: L = mirrored PPU register index ($00-$07)
; Output: A = register value
nes_ppu_cpu_read:
    PROFILE_INC nes_profile_ppu_read
    ld a, l
    cp $02
    jr z, .status
    cp $04
    jr z, .oamdata
    cp $07
    jp z, nes_ppu_read_data
    xor a
    ret

.status:
    ; Approximate NES vblank from the live GBC scanline.
    ldh a, [rLY]
    cp 144
    jr c, .visible_scan

    ; NES clears sprite-0 hit before the next visible frame. Treat host VBlank
    ; as the clear interval so polling loops can observe the old hit disappear.
    ld a, [nes_ppu_status]
    and $3F
    or $80
    jr .status_ready

.visible_scan:
    ld b, a
    ld a, [nes_ppu_status]
    and $3F
    ld e, a

    ; Semantic sprite-0 hit fallback. SMB (and many other early NES games)
    ; waits in NMI for PPUSTATUS bit 6 before changing playfield scroll. We do
    ; not rasterize NES pixels, so synthesize that hit once the host reaches
    ; our matching HUD/playfield split line, provided BG + sprites are enabled
    ; and sprite 0 is not hidden.
    ldh a, [nes_split_line]
    ld c, a
    ld a, b
    cp c
    jr c, .status_from_base

    ld a, [nes_ppumask]
    and $18
    cp $18
    jr nz, .status_from_base

    ld a, [nes_oam_ram]
    cp $EF
    jr nc, .status_from_base

    ld a, e
    or $40
    jr .status_ready

.status_from_base:
    ld a, e

.status_ready:
    ld e, a

    ; PPUSTATUS read clears vblank and the $2005/$2006 write toggle.
    and $7F
    ld [nes_ppu_status], a
    xor a
    ld [nes_ppu_latch], a
    ld a, e
    ret

.oamdata:
    ld hl, nes_oam_ram
    ld a, [nes_oamaddr]
    call nes_add_a_to_hl
    ld a, [hl]
    ret

; Input: L = mirrored PPU register index, E = value
nes_ppu_cpu_write:
    PROFILE_INC nes_profile_ppu_write
    ld a, l
    and $07
    cp $00
    jp z, .ctrl
    cp $01
    jp z, .mask
    cp $03
    jp z, .oamaddr
    cp $04
    jp z, .oamdata_write
    cp $05
    jp z, .scroll
    cp $06
    jp z, .addr
    cp $07
    jp z, nes_ppu_write_data
    ret

.ctrl:
    ; Keep the old/new delta: several PPUCTRL bits have global rendering
    ; semantics that must invalidate already-projected CGB state.
    ld a, [nes_ppuctrl]
    xor e
    ld b, a

    ld a, e
    ld [nes_ppuctrl], a

    ; A number of NES games, including SMB, write the final PPUCTRL *after*
    ; the corresponding $2005 scroll pair. Keep the captured raster state tied
    ; to the most recent scroll pair instead of freezing the nametable select
    ; one write too early.
    ld a, [nes_nmi_active]
    and a
    jr z, .ctrl_capture_done

    ldh a, [nes_scroll_pair_count]
    and a
    jr z, .ctrl_capture_done
    cp $02
    jr nc, .ctrl_capture_bottom

    ; One pair captured: it is still the pending/top candidate.
    ld a, e
    ldh [nes_split_pending_ctrl], a
    jr .ctrl_capture_done

.ctrl_capture_bottom:
    ; Two or more pairs captured: this PPUCTRL belongs to the lower/playfield
    ; state unless a later scroll pair proves otherwise.
    ld a, e
    ldh [nes_split_bottom_ctrl], a

.ctrl_capture_done:

    ; Sprite pattern-table select (bit 3) and sprite size (bit 5) affect every
    ; OAM entry without rewriting NES OAM. Rebuild the projected shadow now
    ; and schedule a fresh hardware OAM commit for the next host VBlank.
    ld a, b
    and $28
    jr z, .ctrl_bg_check
    push bc
    call nes_video_build_oam_shadow
    pop bc
    ld a, $01
    ld [nes_oam_dirty], a

.ctrl_bg_check:
    ; NES background pattern-table select (bit 4) is represented by CGB
    ; attribute bit 3.  Do NOT rewrite live VRAM here: SMB changes PPUCTRL
    ; transiently while a translated NMI is still running, and a long scrolling
    ; NMI can span several host frames.  Publishing that intermediate bit made
    ; the supposedly frozen previous frame show tiles from the wrong pattern
    ; table.  nes_ctrl_dirty below causes the final PPUCTRL value to be
    ; reconciled atomically at the next completed-frame VBlank.

.ctrl_defer:
    ; Base nametable and sprite-size changes should become visible on the same
    ; host frame boundary as SCX/SCY and OAM, not halfway through scanout.
    ld a, $01
    ldh [nes_ctrl_dirty], a
    ret
.mask:
    ld a, e
    ld [nes_ppumask], a

    ; Ordinary NES games use PPUMASK rendering-off as the protection window
    ; for bulk nametable/attribute redraws. Their $2007 writes are published
    ; live, so delaying PPUMASK until a long translated NMI finishes exposes
    ; the entire half-built screen (DK/IC/BF regression).
    ;
    ; Keep the deferred behavior only for the SMB-style vertical-mirroring
    ; stitched raster path, where temporary mask toggles inside one long NMI
    ; must not blank several host frames.
    ld a, [nes_mirroring]
    cp $01
    jr nz, .mask_publish_now

    ldh a, [nes_split_active]
    and a
    jr nz, .mask_defer
    ld a, [nes_hstitch_valid]
    and a
    jr nz, .mask_defer

.mask_publish_now:
    xor a
    ld [nes_mask_dirty], a
    jp nes_video_update_mask

.mask_defer:
    ld a, $01
    ld [nes_mask_dirty], a
    ret
.oamaddr:
    ld a, e
    ld [nes_oamaddr], a
    ret

.oamdata_write:
    ld hl, nes_oam_ram
    ld a, [nes_oamaddr]
    call nes_add_a_to_hl
    ld a, e
    ld [hl], a
    ld a, [nes_oamaddr]
    inc a
    ld [nes_oamaddr], a
    ; Direct OAMDATA writes invalidate any previously projected GBC shadow.
    xor a
    ldh [nes_oam_shadow_ready], a
    ld a, $01
    ld [nes_oam_dirty], a
    ret

.scroll:
    ld a, [nes_ppu_latch]
    and a
    jr nz, .scroll_y

    ; First $2005 write updates virtual X only. Do not touch hardware SCX yet:
    ; committing half of a scroll pair can tear the host frame.
    ld a, e
    ld [nes_ppu_scroll_x], a
    ld a, $01
    ld [nes_ppu_latch], a
    ret

.scroll_y:
    ld a, e
    ld [nes_ppu_scroll_y], a
    xor a
    ld [nes_ppu_latch], a

    ; A complete X/Y pair is ready. During translated NMI, capture the first
    ; pair as the top/HUD scroll and later pairs as the playfield scroll. This
    ; recognizes the common NES raster-split pattern used by Balloon Fight B.
    ld a, [nes_nmi_active]
    and a
    jp z, .scroll_normal

    ldh a, [nes_scroll_pair_count]
    and a
    jr nz, .scroll_capture_bottom

    ; First pair is only a candidate. Do not disturb the currently proven
    ; HUD/playfield split until a second pair confirms this NMI really contains
    ; a split update.
    ld a, [nes_ppu_scroll_x]
    ldh [nes_split_pending_x], a
    ld a, [nes_ppu_scroll_y]
    ldh [nes_split_pending_y], a
    ld a, [nes_ppuctrl]
    ldh [nes_split_pending_ctrl], a
    ld a, $01
    ldh [nes_scroll_pair_count], a
    ldh [nes_scroll_dirty], a
    ret

.scroll_capture_bottom:
    ; Two $2005 pairs in one NMI do NOT automatically imply a raster split.
    ; Ice Climber writes its ordinary scroll pair twice: once when its PPU
    ; update buffer closes and again explicitly before leaving NMI. Treat an
    ; identical X/Y/PPUCTRL tuple as a duplicate, not as a HUD/playfield split.
    ldh a, [nes_split_pending_x]
    ld b, a
    ld a, [nes_ppu_scroll_x]
    cp b
    jr nz, .scroll_confirm_split

    ldh a, [nes_split_pending_y]
    ld b, a
    ld a, [nes_ppu_scroll_y]
    cp b
    jr nz, .scroll_confirm_split

    ldh a, [nes_split_pending_ctrl]
    ld b, a
    ld a, [nes_ppuctrl]
    cp b
    jr nz, .scroll_confirm_split

    ; Duplicate-only NMI.  A proven split is persistent display state:
    ; SMB intermittently writes a duplicate scroll pair between genuine
    ; HUD/playfield updates.  Clearing split_active immediately makes the next
    ; host frame show the HUD map across the entire screen.  Require two
    ; consecutive duplicate-only NMIs before retiring a previously proven split
    ; so ordinary single-scroll games such as Ice Climber still shed stale
    ; transition state quickly.
    ldh a, [nes_split_active]
    and a
    jr z, .duplicate_no_latched_split

    ld a, [nes_split_duplicate_streak]
    inc a
    ld [nes_split_duplicate_streak], a
    cp $02
    jr c, .duplicate_keep_split

    xor a
    ldh [nes_split_active], a
    ld [nes_split_duplicate_streak], a
    jr .duplicate_finish

.duplicate_keep_split:
    ld a, $01
    ldh [nes_split_active], a
    jr .duplicate_finish

.duplicate_no_latched_split:
    xor a
    ld [nes_split_duplicate_streak], a

.duplicate_finish:
    ld a, $01
    ldh [nes_scroll_pair_count], a
    ldh [nes_scroll_dirty], a
    ret

.scroll_confirm_split:
    xor a
    ld [nes_split_duplicate_streak], a

    ; Distinct second pair confirms a real raster split. Commit the pending
    ; first pair atomically as the stable HUD/top state, and this pair as the
    ; playfield/bottom state.
    ldh a, [nes_split_pending_x]
    ldh [nes_split_top_x], a
    ldh a, [nes_split_pending_y]
    ldh [nes_split_top_y], a
    ldh a, [nes_split_pending_ctrl]
    ldh [nes_split_top_ctrl], a

    ld a, [nes_ppu_scroll_x]
    ldh [nes_split_bottom_x], a
    ld a, [nes_ppu_scroll_y]
    ldh [nes_split_bottom_y], a
    ld a, [nes_ppuctrl]
    ldh [nes_split_bottom_ctrl], a

    ld a, $02
    ldh [nes_scroll_pair_count], a
    ld a, $01
    ldh [nes_split_active], a
    ldh [nes_scroll_dirty], a
    ret

.scroll_normal:
    ; A complete X/Y pair is ready. Commit it once at host VBlank.
    ld a, $01
    ldh [nes_scroll_dirty], a
    ret

.addr:
    ld a, [nes_ppu_latch]
    and a
    jr nz, .addr_lo
    ld a, e
    and $3F
    ld [nes_ppu_addr_hi], a
    ld a, $01
    ld [nes_ppu_latch], a
    ret
.addr_lo:
    ld a, e
    ld [nes_ppu_addr_lo], a
    xor a
    ld [nes_ppu_latch], a
    ret

; Read $2007 with the NES delayed-read buffer for non-palette space.
nes_ppu_read_data:
    call nes_ppu_get_addr_hl
    ld a, h
    cp $3F
    jr nc, .palette

    call nes_ppu_read_raw
    ld e, a
    ld a, [nes_ppu_read_buffer]
    ld d, a
    ld a, e
    ld [nes_ppu_read_buffer], a
    call nes_ppu_increment_addr
    ld a, d
    ret

.palette:
    call nes_ppu_read_palette
    ld e, a
    call nes_ppu_increment_addr
    ld a, e
    ret

; Write $2007.
nes_ppu_write_data:
    call nes_ppu_get_addr_hl
    ld a, h
    cp $20
    jr c, .pattern
    cp $3F
    jr nc, .palette

    ; $3000-$3EFF mirrors $2000-$2EFF.
    call nes_ppu_map_nametable_hl
    ld a, e
    ld [hl], a

    ; Only the SMB-style horizontal stitch needs transactional nametable
    ; publication.  It repurposes $9C00 as a synthesized presentation surface,
    ; so exposing future-column parser writes while a translated NMI is still
    ; running can leak half-built world data.
    ;
    ; Ordinary games must not pay that cost. Ice Climber's vertical scrolling
    ; can generate enough $2007 traffic that flushing the global transaction at
    ; the front of host VBlank runs to scanline 30-90+, delaying OAM/scroll/map
    ; commits and producing the large ghost dumps seen in debi8.
    ld a, [nes_nmi_active]
    and a
    jr z, .nametable_sync_now

    ld a, [nes_mirroring]
    cp $01
    jr nz, .nametable_sync_now

    ; Once a vertical-mirroring raster split has established the horizontal
    ; stitched presentation, keep staging even across a transient split-state
    ; wobble while the stitched map itself remains valid.
    ldh a, [nes_split_active]
    and a
    jr nz, .nametable_stage
    ld a, [nes_hstitch_valid]
    and a
    jr z, .nametable_sync_now

.nametable_stage:
    call nes_ppu_stage_nametable_hl
    jp nes_ppu_increment_addr

.nametable_sync_now:
    ; Generic maps have stable physical destinations. Publish them normally;
    ; do not use SMB's persistent published-value cache here because a repeated
    ; NES byte can still need CGB attribute/pattern-bank side effects refreshed.
    ld a, e
    call nes_video_sync_nametable_write
    jp nes_ppu_increment_addr

.pattern:
    ; CHR ROM is read-only for NROM/CNROM. CHR-RAM support comes later.
    jp nes_ppu_increment_addr

.palette:
    call nes_ppu_map_palette_hl
    ld a, e
    and $3F
    ld [hl], a
    call nes_video_sync_palette_write
    jp nes_ppu_increment_addr

; Input HL = physical nametable address $D000-$D7FF.
; Return A=1 on first visit during this translated NMI, A=0 on repeats.
; HL is preserved and WRAM bank 1 is restored before returning.
nes_ppu_nametable_stage_first_visit:
    push hl

    ; E = bit number (low three address bits).
    ld a, l
    and $07
    ld e, a

    ; D800 + (((H & 7) << 5) | (L >> 3)) selects the bitmap byte.
    ld a, l
    srl a
    srl a
    srl a
    ld c, a
    ld a, h
    and $07
    swap a
    add a
    or c
    ld l, a
    ld h, HIGH(nes_nametable_stage_seen)

    ld b, $01
    ld a, e
    and a
    jr z, .stage_mask_ready
.stage_mask_loop:
    sla b
    dec a
    jr nz, .stage_mask_loop
.stage_mask_ready:

    ld a, $06
    ldh [rSVBK], a
    ld a, [hl]
    ld c, a
    and b
    jr nz, .stage_duplicate

    ld a, c
    or b
    ld [hl], a
    ld a, $01
    jr .stage_finish

.stage_duplicate:
    xor a

.stage_finish:
    ld b, a
    ld a, $01
    ldh [rSVBK], a
    ld a, b
    pop hl
    ret

; Append physical virtual nametable address HL ($D000-$D7FF) to the
; current translated-NMI transaction. The tile/attribute value itself is already
; stored in authoritative WRAM, so duplicate addresses are harmless.
nes_ppu_stage_nametable_hl:
    ld a, [nes_nametable_queue_overflow]
    and a
    ret nz

    ; The queue reads the final byte from authoritative nametable WRAM after
    ; RTI, so multiple writes to the same physical PPU address in one NMI need
    ; only one queue entry. Suppress repeats now, outside host VBlank.
    call nes_ppu_nametable_stage_first_visit
    and a
    ret z

    ld a, [nes_nametable_queue_ptr_hi]
    cp $E0
    jr nc, .overflow

    push hl
    ld d, a
    ld a, [nes_nametable_queue_ptr_lo]
    ld e, a
    pop hl

    ld a, l
    ld [de], a
    inc de
    ld a, h
    ld [de], a
    inc de

    ld a, e
    ld [nes_nametable_queue_ptr_lo], a
    ld a, d
    ld [nes_nametable_queue_ptr_hi], a
    ret

.overflow:
    ld a, $01
    ld [nes_nametable_queue_overflow], a
    ret

nes_ppu_get_addr_hl:
    ld a, [nes_ppu_addr_hi]
    ld h, a
    ld a, [nes_ppu_addr_lo]
    ld l, a
    ret

; Raw PPU read. Input HL = 14-bit PPU address.
nes_ppu_read_raw:
    ld a, h
    cp $20
    jr c, .pattern
    cp $3F
    jr nc, .palette

    call nes_ppu_map_nametable_hl
    ld a, [hl]
    ret

.pattern:
    ; Each 8 KiB NES CHR bank gets its own GBC ROM bank starting at bank 3.
    ld a, [nes_chr_bank]
    add $03
    ld [$2000], a

    ; Map PPU $0000-$1FFF to GBC ROMX $4000-$5FFF.
    ld a, h
    and $1F
    or $40
    ld h, a
    ld a, [hl]
    push af
    call nes_restore_code_bank
    pop af
    ret

.palette:
    jp nes_ppu_read_palette

nes_ppu_read_palette:
    call nes_ppu_map_palette_hl
    ld a, [hl]
    ret

; Input HL = $2000-$3EFF. Output HL = WRAMX bank 1 physical nametable.
nes_ppu_map_nametable_hl:
    ld a, h
    cp $30
    jr c, .normalized
    sub $10
    ld h, a
.normalized:
    ld a, $01
    ldh [rSVBK], a

    ; Inner offset high bits are A9-A8.
    ld a, h
    and $03
    ld c, a

    ld a, [nes_mirroring]
    cp $01
    jr z, .vertical

    ; Horizontal: logical 0/1 -> physical 0, logical 2/3 -> physical 1.
    ld a, h
    and $08
    srl a
    jr .combine

.vertical:
    ; Vertical: logical 0/2 -> physical 0, logical 1/3 -> physical 1.
    ld a, h
    and $04

.combine:
    or c
    or $D0
    ld h, a
    ret

; Input HL = $3F00-$3FFF. Output HL = nes_palette_ram + mirrored index.
nes_ppu_map_palette_hl:
    ld a, l
    and $1F
    ld l, a

    ; $3F10/$14/$18/$1C mirror universal background entries.
    cp $10
    jr c, .mapped
    ld a, l
    and $03
    jr nz, .mapped
    ld a, l
    sub $10
    ld l, a

.mapped:
    ld a, l
    add $30
    ld l, a
    ld h, $C8
    ret

nes_ppu_increment_addr:
    ld a, [nes_ppuctrl]
    bit 2, a
    jr nz, .by_32

    ld a, [nes_ppu_addr_lo]
    inc a
    ld [nes_ppu_addr_lo], a
    ret nz
    ld a, [nes_ppu_addr_hi]
    inc a
    and $3F
    ld [nes_ppu_addr_hi], a
    ret

.by_32:
    ld a, [nes_ppu_addr_lo]
    add $20
    ld [nes_ppu_addr_lo], a
    jr nc, .mask_hi
    ld a, [nes_ppu_addr_hi]
    inc a
    jr .store_hi
.mask_hi:
    ld a, [nes_ppu_addr_hi]
.store_hi:
    and $3F
    ld [nes_ppu_addr_hi], a
    ret


; $4014 OAM DMA. Input A = source page.
nes_oam_dma:
    ld h, a
    ld l, $00
    ld d, $C9
    ld a, [nes_oamaddr]
    ld e, a

    ; The common case is DMA from NES internal RAM (usually page $02).
    ; Map the mirrored source once and copy all 256 bytes directly instead of
    ; calling the generic CPU bus reader 256 times every frame.
    ld a, h
    cp $20
    jr nc, .generic

    PROFILE_INC nes_profile_oam_dma_fast
    and $07
    or $C0
    ld h, a
    ld b, $00
.fast_loop:
    ld a, [hli]
    ld [de], a
    inc e
    inc b
    jr nz, .fast_loop
    jr .dirty

.generic:
    PROFILE_INC nes_profile_oam_dma_generic
    ; Unusual DMA sources may touch PPU/APU/PRG space, so retain full bus
    ; semantics for them.
.generic_loop:
    push hl
    push de
    call nes_cpu_read
    pop de
    pop hl

    ld [de], a
    inc e
    inc l
    jr nz, .generic_loop

.dirty:
    ; Projection is deliberately done here, outside host VBlank. VBlank only
    ; copies the finished 160-byte shadow to hardware OAM.
    call nes_video_build_oam_shadow
    ld a, $01
    ld [nes_oam_dirty], a
    ret
