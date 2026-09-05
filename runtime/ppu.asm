; NES PPU register semantics backed by GBC WRAM/ROM banks.
; This is a semantic model, not cycle-accurate PPU emulation.

SECTION "NES PPU helpers", ROM0

; Input: L = mirrored PPU register index ($00-$07)
; Output: A = register value
nes_ppu_cpu_read:
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
    ; Approximate NES PPUSTATUS from the live GBC scanline.
    ; Bit 7 (vblank) follows host VBlank. Bit 6 (sprite-0 hit) is synthesized
    ; once the host scanline reaches virtual NES sprite 0's Y coordinate while
    ; both BG and sprite rendering are enabled. We do not yet test pixel overlap,
    ; but this preserves the clear-then-hit timing behavior used by raster waits.
    ldh a, [rLY]
    ld b, a

    ld a, [nes_ppu_status]
    and $3F
    ld e, a

    ld a, b
    cp 144
    jr c, .visible
    ld a, e
    or $80
    ld e, a
    jr .status_ready

.visible:
    ld a, [nes_ppumask]
    and $18
    cp $18
    jr nz, .status_ready

    ; NES OAM stores sprite Y as top-1. Large Y values are hidden/offscreen.
    ld a, [nes_oam_ram]
    cp $EF
    jr nc, .status_ready
    inc a
    ld c, a

    ld a, b
    cp c
    jr c, .status_ready
    ld a, e
    or $40
    ld e, a

.status_ready:
    ; Reading PPUSTATUS clears vblank and the $2005/$2006 write toggle.
    ld a, [nes_ppu_status]
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
    ld a, l
    and $07
    cp $00
    jr z, .ctrl
    cp $01
    jr z, .mask
    cp $03
    jr z, .oamaddr
    cp $04
    jr z, .oamdata_write
    cp $05
    jr z, .scroll
    cp $06
    jr z, .addr
    cp $07
    jp z, nes_ppu_write_data
    ret

.ctrl:
    ld a, e
    ld [nes_ppuctrl], a
    call nes_video_update_ctrl
    ret
.mask:
    ld a, e
    ld [nes_ppumask], a
    call nes_video_update_mask
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
    ret

.scroll:
    ld a, [nes_ppu_latch]
    and a
    jr nz, .scroll_y
    ld a, e
    ld [nes_ppu_scroll_x], a
    ldh [rSCX], a
    ld a, $01
    ld [nes_ppu_latch], a
    ret
.scroll_y:
    ld a, e
    ld [nes_ppu_scroll_y], a
    ldh [rSCY], a
    xor a
    ld [nes_ppu_latch], a
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

.loop:
    push hl
    push de
    call nes_cpu_read
    pop de
    pop hl

    ld [de], a
    inc e
    inc l
    jr nz, .loop

    jp nes_video_sync_oam
