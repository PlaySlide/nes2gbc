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
    ret nc

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

; Reflect NES base-nametable selection into GBC BG map selection.
nes_video_update_ctrl:
    ldh a, [rLCDC & $FF]
    and $F7
    ld b, a

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
