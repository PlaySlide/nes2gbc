; GBC video bridge for the virtual NES PPU.
; Correctness-first: expensive operations may wait for VBlank or briefly disable LCD.

SECTION "NES video bridge", ROM0

nes_video_init:
    ; Enter a safe LCD-off setup window once at boot.
    ldh a, [rLCDC & $FF]
    bit 7, a
    jr z, .initial_lcd_off
.wait_initial_vblank:
    ldh a, [rLY & $FF]
    cp 144
    jr c, .wait_initial_vblank
    ldh a, [rLCDC & $FF]
    and $7F
    ldh [rLCDC & $FF], a
.initial_lcd_off:

    call nes_upload_chr_bank

    ; LCD is off after upload: clear both BG maps and their CGB attributes.
    xor a
    ldh [rVBK & $FF], a
    ld hl, $9800
    ld bc, $0800
    call nes_video_fill_zero

    ld a, $01
    ldh [rVBK & $FF], a
    ld hl, $9800
    ld bc, $0800
    call nes_video_fill_zero

    xor a
    ldh [rVBK & $FF], a
    ldh [rSCX & $FF], a
    ldh [rSCY & $FF], a

    ; Palette 0: white -> light gray -> dark gray -> black.
    ld a, $80
    ldh [rBGPI & $FF], a
    ld a, $FF
    ldh [rBGPD & $FF], a
    ld a, $7F
    ldh [rBGPD & $FF], a
    ld a, $B5
    ldh [rBGPD & $FF], a
    ld a, $56
    ldh [rBGPD & $FF], a
    ld a, $4A
    ldh [rBGPD & $FF], a
    ld a, $29
    ldh [rBGPD & $FF], a
    xor a
    ldh [rBGPD & $FF], a
    ldh [rBGPD & $FF], a

    ; LCD on, BG on, unsigned tile IDs, map $9800.
    ld a, $91
    ldh [rLCDC & $FF], a
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
    ldh a, [rLCDC & $FF]
    ld [nes_saved_lcdc], a
    bit 7, a
    jr z, .lcd_off

.wait_vblank:
    ldh a, [rLY & $FF]
    cp 144
    jr c, .wait_vblank

    ldh a, [rLCDC & $FF]
    and $7F
    ldh [rLCDC & $FF], a

.lcd_off:
    ld a, [nes_chr_gbc_bank_base]
    ld b, a
    ld a, [nes_chr_bank]
    add b
    ld [$2000], a

    xor a
    ldh [rVBK & $FF], a
    ld hl, $4000
    ld de, $8000
    ld bc, $1000
    call nes_video_copy

    ld a, $01
    ldh [rVBK & $FF], a
    ld hl, $5000
    ld de, $8000
    ld bc, $1000
    call nes_video_copy

    xor a
    ldh [rVBK & $FF], a

    call nes_restore_code_bank
    ld a, [nes_saved_lcdc]
    ldh [rLCDC & $FF], a
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

; Wait until VRAM is writable. This intentionally stalls rather than losing a write.
nes_video_wait_vram:
    ldh a, [rLCDC & $FF]
    bit 7, a
    ret z
.wait:
    ldh a, [rLY & $FF]
    cp 144
    jr c, .wait
    ret

; Input: HL = physical virtual nametable address ($D000-$D7FF), A = written byte.
nes_video_sync_nametable_write:
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
    call nes_video_wait_vram

    ; GBC map high byte is $98 + physical-table/inner-page index.
    ld a, h
    and $07
    add $98
    ld d, a
    ld e, l

    ; Tile ID.
    xor a
    ldh [rVBK & $FF], a
    ld a, c
    ld [de], a

    ; CGB attribute: palette 0, VRAM bank follows NES BG pattern-table select.
    ld a, $01
    ldh [rVBK & $FF], a
    ld a, [nes_ppuctrl]
    and $10
    srl a
    ld [de], a

    xor a
    ldh [rVBK & $FF], a
    ret

.attribute:
    ld a, c
    jp nes_video_sync_attribute_write

; Expand one NES attribute byte into sixteen CGB tile attributes.
; Input: HL = physical attribute address ($D3C0-$D3FF or $D7C0-$D7FF), A = attribute byte.
nes_video_sync_attribute_write:
    ld b, a
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
    ldh [rVBK & $FF], a

    call nes_video_attr_top_row
    call nes_video_attr_top_row
    call nes_video_attr_bottom_row
    call nes_video_attr_bottom_row

    xor a
    ldh [rVBK & $FF], a
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
    or $80
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
    ldh [rBGPI & $FF], a
    ld a, e
    ldh [rBGPD & $FF], a
    ld a, d
    ldh [rBGPD & $FF], a
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
    or $80
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
    ldh [rOBPI & $FF], a
    ld a, e
    ldh [rOBPD & $FF], a
    ld a, d
    ldh [rOBPD & $FF], a
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

; Project virtual NES OAM into the first 40 CGB sprites.
nes_video_sync_oam:
    call nes_video_wait_vram
    ld hl, nes_oam_ram
    ld de, $FE00
    ld b, 40

.loop:
    ; NES Y is top minus one; GBC OAM Y is screen Y plus 16.
    ld a, [hli]
    inc a
    add $10
    ld [de], a
    inc de

    ld a, [hli]
    ld c, a
    ld a, [hli]
    ld [nes_sprite_attr_tmp], a

    ; X coordinate.
    ld a, [hli]
    add $08
    ld [de], a
    inc de

    ; Tile number and pattern-table bank.
    ld a, [nes_ppuctrl]
    bit 5, a
    jr z, .sprite_8x8

    ld a, c
    and $FE
    ld [de], a
    inc de

    bit 0, c
    ld a, $00
    jr z, .bank_ready
    ld a, $08
    jr .bank_ready

.sprite_8x8:
    ld a, c
    ld [de], a
    inc de
    ld a, [nes_ppuctrl]
    and $08

.bank_ready:
    ld [nes_sprite_bank_tmp], a

    ; Palette, CGB VRAM bank, priority, H flip, V flip.
    ld a, [nes_sprite_attr_tmp]
    and $03
    ld c, a
    ld a, [nes_sprite_bank_tmp]
    or c
    ld c, a

    ld a, [nes_sprite_attr_tmp]
    bit 5, a
    jr z, .no_priority
    ld a, c
    or $80
    ld c, a
.no_priority:
    ld a, [nes_sprite_attr_tmp]
    bit 6, a
    jr z, .no_hflip
    ld a, c
    or $20
    ld c, a
.no_hflip:
    ld a, [nes_sprite_attr_tmp]
    bit 7, a
    jr z, .no_vflip
    ld a, c
    or $40
    ld c, a
.no_vflip:
    ld a, c
    ld [de], a
    inc de

    dec b
    jr nz, .loop
    ret

; Reflect PPUMASK BG/sprite visibility into LCDC bits 0/1.
nes_video_update_mask:
    ldh a, [rLCDC & $FF]
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
    ldh [rLCDC & $FF], a
    ret

; Reflect NES base-nametable selection and sprite size into GBC LCDC.
nes_video_update_ctrl:
    ldh a, [rLCDC & $FF]
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
    ldh [rLCDC & $FF], a
    ret
