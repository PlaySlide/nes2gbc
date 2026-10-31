; FIT_SCREEN v2 (opt-in, make generate FIT_SCREEN=1): show the whole NES
; 256x240 screen as 128x120 on the CGB.
;
; * Every NES tile is shrunk to a 4x4 image (top-left of an 8x8 CGB tile):
;   CHR-ROM at build time (assets::fit_shrink_tile), CHR-RAM here at commit
;   time with the same rule (row a over row b, left column over right one,
;   packed with the 256-byte nibble table nes_fit_pack_table).
;   Shrunk tiles live at $8000 in VRAM banks 0/1 (sprites use them directly).
; * The displayed BG pattern table's shrunk 4x4 tiles are cached in WRAM 7
;   ($D800, 8 bytes per tile). One CGB BG "slot" tile = 2x2 NES tiles = one
;   NES attribute quadrant, so the 16x15 slots carry the NES palettes exactly.
;   Slots 0-127 live at VRAM bank 0 $9000, 128-239 at bank 1 $9000 (signed
;   tile ids, LCDC.4 = 0); the map at $9800 is static except the palette.
; * Nametable / attribute writes to the displayed physical table mark slots
;   in a 240-bit dirty bitmap. Each host frame composes up to NES2GBC_FIT_BUDGET
;   dirty slots (unrolled) into a WRAM 7 stage; the next VBlank GDMAs them in.
; * Known limit (step 4 = scroll ring): only whole pages are shown. The page
;   is the logical table that holds most of the visible NES screen.

IF !DEF(NES2GBC_FIT_BUDGET)
DEF NES2GBC_FIT_BUDGET EQU 24
ENDC
DEF FIT_rHDMA1 EQU $FF51
DEF FIT_rHDMA2 EQU $FF52
DEF FIT_rHDMA3 EQU $FF53
DEF FIT_rHDMA4 EQU $FF54
DEF FIT_rHDMA5 EQU $FF55
DEF FIT_CACHE_HI EQU $D8

SECTION "NES fit state", WRAM0
nes_fit_dirty:      ds 30   ; 240 slot bits
nes_fit_any:        ds 1    ; nonzero: some dirty bit may be set
nes_fit_page:       ds 1    ; displayed physical nametable high byte ($D0/$D4)
nes_fit_bgpt:       ds 1    ; pattern table held by the cache (0/1, $FF = stale)
nes_fit_stage_n:    ds 1    ; composed slots waiting for VBlank
nes_fit_chr_any:    ds 1
nes_fit_commit:     ds 1    ; a completed NES frame was committed this VBlank    ; CHR-RAM tiles waiting for re-shrink
nes_fit_tiles:      ds 5    ; compose temp: 4 tile ids + palette
nes_fit_half_bank:  ds 2    ; CHR-ROM source of PT0/PT1 (ROM bank)
nes_fit_half_hi:    ds 2    ; ... and its address high byte ($40/$50)
nes_fit_tmp:        ds 8    ; one shrunk tile
nes_fit_chr_dirty:  ds 64   ; 512 CHR-RAM tile bits

SECTION "NES fit stage", WRAMX[$D100], BANK[7]
nes_fit_stage:      ds NES2GBC_FIT_BUDGET * 16
nes_fit_stage_slot: ds NES2GBC_FIT_BUDGET
nes_fit_stage_attr: ds NES2GBC_FIT_BUDGET

SECTION "NES fit cache", WRAMX[$D800], BANK[7]
nes_fit_cache:      ds $800

SECTION "NES fit pack table", ROMX, BANK[255], ALIGN[8]
; bits 7,5,3,1 -> 7,6,5,4
nes_fit_pack_table:
FOR X, 256
    db ((X & $80) | ((X & $20) << 1) | ((X & $08) << 2) | ((X & $02) << 3)) & $F0
ENDR
SECTION "NES fit bits", ROM0
nes_fit_bit_table:
    db $01, $02, $04, $08, $10, $20, $40, $80

SECTION "NES fit init", ROMX, BANK[255]

; LCD off. Static slot map, blank border, cleared slot tiles, letterbox scroll.
nes_fit_init_x:
    ld a, $01
    ldh [rVBK], a
    ld hl, $9000
    ld bc, $0800
    call nes_video_fill_zero
    xor a
    ldh [rVBK], a
    ld hl, $9000
    ld bc, $0800
    call nes_video_fill_zero
    ; tile ids (bank 0) then attributes (bank 1)
    ld hl, $9800
    ld c, 0            ; row
.row:
    ld b, 0            ; col
.col:
    ld a, c
    cp 15
    jr nc, .blank
    ld a, b
    cp 16
    jr nc, .blank
    ld a, c
    swap a
    or b
    and $7F
    jr .put
.blank:
    ld a, $7F
.put:
    ld [hl], a
    ; attribute: bank bit = slot >= 128 (rows 8-14) or blank
    ld a, $01
    ldh [rVBK], a
    ld a, c
    cp 15
    jr nc, .attr_b1
    ld a, b
    cp 16
    jr nc, .attr_b1
    ld a, c
    cp 8
    jr nc, .attr_b1
    xor a
    jr .attr_put
.attr_b1:
    ld a, $08
.attr_put:
    ld [hli], a
    xor a
    ldh [rVBK], a
    inc b
    ld a, b
    cp 32
    jr nz, .col
    inc c
    ld a, c
    cp 32
    jr nz, .row

    ld a, $F0
    ldh [rSCX], a
    ld a, $F4
    ldh [rSCY], a
    xor a
    ld [nes_fit_stage_n], a
    ld [nes_fit_chr_any], a
    ld hl, nes_fit_chr_dirty
    ld b, 64
.clr:
    ld [hli], a
    dec b
    jr nz, .clr
    dec a
    ld [nes_fit_page], a
    ld [nes_fit_bgpt], a
    jp nes_fit_mark_all

SECTION "NES fit hot", ROM0
nes_fit_mark_all:
    ld hl, nes_fit_dirty
    ld a, $FF
    ld b, 30
.loop:
    ld [hli], a
    dec b
    jr nz, .loop
    ld [nes_fit_any], a
    ret

; A = slot (0-239). Clobbers A, BC, HL.
nes_fit_mark_slot:
    ld c, a
    and $07
    add LOW(nes_fit_bit_table)
    ld l, a
    ld h, HIGH(nes_fit_bit_table)
    ld b, [hl]
    ld a, c
    rrca
    rrca
    rrca
    and $1F
    add LOW(nes_fit_dirty)
    ld l, a
    adc HIGH(nes_fit_dirty)
    sub l
    ld h, a
    ld a, [hl]
    or b
    ld [hl], a
    ld a, $01
    ld [nes_fit_any], a
    ret

; HL = physical nametable byte just changed ($D000-$D7FF). Preserves all but A/F.
nes_fit_nt_write:
    push bc
    push de
    push hl
    ld a, h
    and $FC
    ld b, a
    ld a, [nes_fit_page]
    cp b
    jr nz, .out
    ld a, h
    and $03
    ld h, a
    cp $03
    jr nz, .tile
    ld a, l
    cp $C0
    jr nc, .attr
.tile:
    ; slot = h<<6 | (l>>6)<<4 | (l & $1E)>>1
    ld a, l
    and $1E
    rrca
    ld e, a
    ld a, l
    rlca
    rlca
    and $03
    swap a
    or e
    ld e, a
    ld a, h
    rrca
    rrca
    and $C0
    or e
    call nes_fit_mark_slot
.out:
    pop hl
    pop de
    pop bc
    ret
.attr:
    ; idx = l - $C0: ay = idx>>3, ax = idx&7; base slot = ay*32 + ax*2
    ld a, l
    sub $C0
    ld d, a
    and $07
    add a
    ld e, a
    ld a, d
    and $38
    add a
    add a
    or e
    ld e, a
    call nes_fit_mark_slot
    ld a, e
    inc a
    call nes_fit_mark_slot
    ld a, d
    and $38
    cp $38
    jr z, .out          ; ay = 7: slot row 15 does not exist
    ld a, e
    add 16
    call nes_fit_mark_slot
    ld a, e
    add 17
    call nes_fit_mark_slot
    jr .out

; CHR-RAM byte written at pattern address HL ($0000-$1FFF). Preserves all but A/F.
nes_fit_chr_write:
    push bc
    push hl
    ; tile = HL >> 4 (9 bits); byte = tile >> 3, bit = tile & 7
    ld a, l
    swap a
    and $0F
    ld c, a
    ld a, h
    and $0F
    swap a
    or c
    ld c, a              ; C = tile & $FF
    and $07
    add LOW(nes_fit_bit_table)
    ld l, a
    ld a, HIGH(nes_fit_bit_table)
    ld h, a
    ld b, [hl]
    pop hl
    push hl
    ; byte index = (HL >> 7) & $3F
    ld a, l
    rlca
    and $01
    ld c, a
    ld a, h
    and $1F
    add a
    or c
    add LOW(nes_fit_chr_dirty)
    ld l, a
    adc HIGH(nes_fit_chr_dirty)
    sub l
    ld h, a
    ld a, [hl]
    or b
    ld [hl], a
    ld a, $01
    ld [nes_fit_chr_any], a
    pop hl
    pop bc
    ret

; Shrink one raw NES CHR-RAM tile. A = tile (low 8 bits), B = pattern table.
; Output nes_fit_tmp[0..7]. Leaves SVBK changed (caller restores).
SECTION "NES fit helper", ROMX, BANK[255]
nes_fit_shrink_tile:
    ld l, a
    ld a, b
    add $02
    ldh [rSVBK], a
    ld a, l
    swap a
    ld l, a
    and $0F
    or $D0
    ld h, a
    ld a, l
    and $F0
    ld l, a              ; HL = raw tile
    ld de, nes_fit_tmp
    ld b, 4
.row:
    ; C = lo merged, then hi merged; n kept on stack
    push bc
    ld a, [hl]           ; lo_a
    set 3, l
    or [hl]              ; | hi_a = n_a
    cpl
    ld c, a              ; ~n_a
    inc l
    ld a, [hl]           ; hi_b
    and c
    dec l
    or [hl]              ; hi = hi_a | (hi_b & ~n_a)
    ld b, a
    res 3, l
    inc l
    ld a, [hl]           ; lo_b
    and c
    dec l
    or [hl]              ; lo
    ld c, a
    ; n = lo | hi; keep mask = $AA | ($55 & ~(n>>1))  (applied as below)
    or b
    srl a
    cpl
    and $55
    push hl
    ld h, a              ; H = right-bit keep mask
    ld a, c
    and h
    add a
    ld l, a
    ld a, c
    and $AA
    or l
    push hl
    ld l, a
    ld h, HIGH(nes_fit_pack_table)
    ld a, [hl]
    ld [de], a
    inc de
    pop hl
    ld a, b
    and h
    add a
    ld l, a
    ld a, b
    and $AA
    or l
    ld l, a
    ld h, HIGH(nes_fit_pack_table)
    ld a, [hl]
    ld [de], a
    inc de
    pop hl
    inc l
    inc l
    pop bc
    dec b
    jr nz, .row
    ret

; Re-shrink every dirty CHR-RAM tile: VRAM $8000 copy (sprites) and the BG
; cache when it holds that pattern table. Runs from the ISR tail.
nes_fit_process_chr:
    xor a
    ld [nes_fit_chr_any], a
    ld c, 0               ; byte index 0..63
.byte:
    ld a, c
    add LOW(nes_fit_chr_dirty)
    ld l, a
    adc HIGH(nes_fit_chr_dirty)
    sub l
    ld h, a
    ld a, [hl]
    and a
    jr z, .next_byte
    ld [hl], 0
    ld b, 0               ; bit
.bit:
    rrca
    jr nc, .next_bit
    push af
    push bc
    ; tile = c*8 + b (9 bits): table = c>>5
    ld a, c
    add a
    add a
    add a
    or b
    ld e, a               ; E = tile low 8
    ld a, c
    swap a
    rrca
    and $01
    ld d, a               ; D = pattern table
    push de
    ld b, d
    ld a, e
    call nes_fit_shrink_tile
    pop de
    ld a, $01
    ldh [rSVBK], a
    call nes_fit_store_tile
    pop bc
    pop af
.next_bit:
    inc b
    bit 3, b
    jr z, .bit
.next_byte:
    inc c
    ld a, c
    cp 64
    jr nz, .byte
    ret

; D = pattern table, E = tile; nes_fit_tmp holds the shrunk rows.
; Returns with the BG cache updated (and all slots dirty) when it holds table D.
nes_fit_store_tile:
    ; VRAM: bank D, $8000 + E*16 (+8 bytes: even 8x16 sprites use rows 4-7)
    ld a, e
    swap a
    ld l, a
    and $0F
    or $80
    ld h, a
    ld a, l
    and $F0
    ld l, a
    ld a, [nes_ppuctrl]
    bit 5, a
    jr z, .row0
    bit 0, e
    jr nz, .row0
    set 3, l
.row0:
    ld c, d
    push de
    ld de, nes_fit_tmp
    ld b, 4
.v:
    call nes_video_wait_vram
    ld a, c
    ldh [rVBK], a
    ld a, [de]
    ld [hli], a
    inc de
    ld a, [de]
    ld [hli], a
    inc de
    xor a
    ldh [rVBK], a
    dec b
    jr nz, .v
    pop de
    ld a, [nes_fit_bgpt]
    cp d
    ret nz
    ; cache: $D800 + E*8
    ld a, e
    rlca
    rlca
    rlca
    ld l, a
    and $07
    add FIT_CACHE_HI
    ld h, a
    ld a, l
    and $F8
    ld l, a
    ld a, $07
    ldh [rSVBK], a
    ld de, nes_fit_tmp
    ld b, 8
.c:
    ld a, [de]
    ld [hli], a
    inc de
    dec b
    jr nz, .c
    ld a, $01
    ldh [rSVBK], a
    jp nes_fit_mark_all

; Fill the BG cache with pattern table A (0/1).
nes_fit_refill_cache:
    ld [nes_fit_bgpt], a
    ld d, a
    ld a, [nes_chr_is_ram]
    and a
    jr nz, .ram
    jp nes_fit_refill_rom
.ram:
    ld e, 0
.ram_tile:
    push de
    ld b, d
    ld a, e
    call nes_fit_shrink_tile
    pop de
    ld a, e
    rlca
    rlca
    rlca
    ld l, a
    and $07
    add FIT_CACHE_HI
    ld h, a
    ld a, l
    and $F8
    ld l, a
    ld a, $07
    ldh [rSVBK], a
    push de
    ld de, nes_fit_tmp
    REPT 8
    ld a, [de]
    ld [hli], a
    inc de
    ENDR
    pop de
    inc e
    jr nz, .ram_tile
    ld a, $01
    ldh [rSVBK], a
    jp nes_fit_mark_all

; Record the CHR-ROM source of one pattern table half: D = half, A = ROM bank,
; H = address high byte. Preserves A/HL/DE/BC.
SECTION "NES fit rom0", ROM0
; CHR-ROM cache refill (ROM0: maps the converted CHR bank). D = table.
; Returns with helper bank $FF mapped.
nes_fit_refill_rom:
    ; CHR-ROM: every shrunk tile's first 8 bytes from the converted bank.
    ld a, d
    and a
    ld a, [nes_fit_half_bank]
    ld b, a
    ld a, [nes_fit_half_hi]
    jr z, .r_src
    ld a, [nes_fit_half_bank + 1]
    ld b, a
    ld a, [nes_fit_half_hi + 1]
.r_src:
    ld h, a
    ld l, 0
    ld a, b
    ld [$2000], a
    ld a, $07
    ldh [rSVBK], a
    ld de, nes_fit_cache
.r_tile:
    REPT 8
    ld a, [hli]
    ld [de], a
    inc de
    ENDR
    ld a, l
    add 8
    ld l, a
    jr nc, .r_nc
    inc h
.r_nc:
    ld a, d
    cp HIGH(nes_fit_cache) + 8
    jr nz, .r_tile
    ld a, $01
    ldh [rSVBK], a
    ld a, $FF
    ld [$2000], a
    jp nes_fit_mark_all

nes_fit_note_half:
    push bc
    push hl
    ld b, a
    ld a, d
    and a
    ld a, b
    jr nz, .h1
    ld [nes_fit_half_bank], a
    ld a, h
    ld [nes_fit_half_hi], a
    jr .done
.h1:
    ld [nes_fit_half_bank + 1], a
    ld a, h
    ld [nes_fit_half_hi + 1], a
.done:
    ld a, $FF
    ld [nes_fit_bgpt], a
    ld a, b
    pop hl
    pop bc
    ret

; Compose slot A into the stage at DE (16-byte aligned, SVBK any).
; Returns A = CGB attribute byte (palette + VRAM bank). DE advances 16.
SECTION "NES fit compose", ROMX, BANK[255]
nes_fit_compose_slot:
    ld c, a
    ld a, $01
    ldh [rSVBK], a
    ; NT address = page + (cy>>2) : (cy&3)<<6 | cx*2
    ld a, c
    swap a
    and $0F
    ld b, a               ; B = cy
    rrca
    rrca
    and $03
    ld h, a
    ld a, [nes_fit_page]
    add h
    ld h, a
    ld a, b
    and $03
    rrca
    rrca
    ld l, a
    ld a, c
    and $0F
    add a
    or l
    ld l, a
    ld a, [hli]
    ld [nes_fit_tiles + 0], a
    ld a, [hl]
    ld [nes_fit_tiles + 1], a
    ld a, l
    add 31
    ld l, a
    ld a, [hli]
    ld [nes_fit_tiles + 2], a
    ld a, [hl]
    ld [nes_fit_tiles + 3], a
    ; attribute: page + $3C0 + (cy>>1)*8 + (cx>>1)
    ld a, [nes_fit_page]
    add $03
    ld h, a
    ld a, b
    and $0E
    add a
    add a
    ld l, a
    ld a, c
    and $0F
    rrca
    and $07
    or l
    or $C0
    ld l, a
    ld a, [hl]
    ; shift = (cy&1)*4 + (cx&1)*2
    bit 0, b
    jr z, .no_ys
    swap a
.no_ys:
    bit 0, c
    jr z, .no_xs
    rrca
    rrca
.no_xs:
    and $03
    bit 7, c
    jr z, .bank0
    or $08
.bank0:
    ld [nes_fit_tiles + 4], a

    ld a, $07
    ldh [rSVBK], a
    ld a, [nes_fit_tiles + 0]
    call .ptr
    push hl
    ld a, [nes_fit_tiles + 1]
    call .ptr
    ld b, h
    ld c, l
    pop hl
    REPT 8
    ld a, [bc]
    swap a
    or [hl]
    ld [de], a
    inc l
    inc c
    inc e
    ENDR
    ld a, [nes_fit_tiles + 2]
    call .ptr
    push hl
    ld a, [nes_fit_tiles + 3]
    call .ptr
    ld b, h
    ld c, l
    pop hl
    REPT 7
    ld a, [bc]
    swap a
    or [hl]
    ld [de], a
    inc l
    inc c
    inc e
    ENDR
    ld a, [bc]
    swap a
    or [hl]
    ld [de], a
    inc de
    ld a, [nes_fit_tiles + 4]
    ret
.ptr:
    ; HL = cache + A*8
    rlca
    rlca
    rlca
    ld l, a
    and $07
    add FIT_CACHE_HI
    ld h, a
    ld a, l
    and $F8
    ld l, a
    ret

; ISR tail (any time, VRAM not needed except CHR-RAM tile rows): refresh the
; cache, then compose up to NES2GBC_FIT_BUDGET dirty slots into the stage.
nes_fit_compose_budget:
    ldh a, [rSVBK]
    push af
    ld a, [nes_fit_chr_any]
    and a
    call nz, nes_fit_process_chr
    ld a, [nes_ppuctrl]
    and $10
    swap a
    ld b, a
    ld a, [nes_fit_bgpt]
    cp b
    ld a, b
    call nz, nes_fit_refill_cache
    ld a, [nes_fit_stage_n]
    and a
    jr nz, .out
    ld a, [nes_fit_any]
    and a
    jr z, .out
    ld a, [nes_fit_page]
    cp $FF
    jr z, .out
    ld de, nes_fit_stage
    ld c, 0              ; byte index
.byte:
    ld a, c
    add LOW(nes_fit_dirty)
    ld l, a
    adc HIGH(nes_fit_dirty)
    sub l
    ld h, a
    ld a, [hl]
    and a
    jr z, .next_byte
    ld b, 0
.bit:
    srl a
    jr nc, .next_bit
    push af
    push bc
    push hl
    ; clear the bit
    ld a, b
    add LOW(nes_fit_bit_table)
    ld l, a
    ld h, HIGH(nes_fit_bit_table)
    ld a, [hl]
    cpl
    pop hl
    push hl
    and [hl]
    ld [hl], a
    ld a, c
    add a
    add a
    add a
    or b
    push af
    call nes_fit_compose_slot
    ld b, a              ; attr
    ld a, [nes_fit_stage_n]
    ld c, a
    add LOW(nes_fit_stage_attr)
    ld l, a
    adc HIGH(nes_fit_stage_attr)
    sub l
    ld h, a
    ld [hl], b
    ld a, c
    add LOW(nes_fit_stage_slot)
    ld l, a
    adc HIGH(nes_fit_stage_slot)
    sub l
    ld h, a
    pop af
    ld [hl], a
    ld a, c
    inc a
    ld [nes_fit_stage_n], a
    pop hl
    pop bc
    cp NES2GBC_FIT_BUDGET
    jr z, .full
    pop af
.next_bit:
    inc b
    and a
    jr nz, .bit
.next_byte:
    inc c
    ld a, c
    cp 30
    jr nz, .byte
    xor a
    ld [nes_fit_any], a
.out:
    pop af
    ldh [rSVBK], a
    ret
.full:
    pop af
    jr .out

; VBlank: GDMA every staged slot into VRAM and set its map palette.
nes_fit_flush:
    ld a, [nes_fit_stage_n]
    and a
    ret z
    ld b, a
    ldh a, [rSVBK]
    push af
    ld a, $07
    ldh [rSVBK], a
    ld c, 0
    ld de, nes_fit_stage
.loop:
    ldh a, [rLY]
    cp 144
    jr c, .late
    ; slot
    ld a, c
    add LOW(nes_fit_stage_slot)
    ld l, a
    adc HIGH(nes_fit_stage_slot)
    sub l
    ld h, a
    ld a, [hl]
    push af
    rlca
    and $01
    ldh [rVBK], a
    ld a, d
    ldh [FIT_rHDMA1], a
    ld a, e
    ldh [FIT_rHDMA2], a
    pop af
    push af
    and $7F
    swap a
    ld l, a
    and $0F
    or $10              ; $9000 + id*16 -> HDMA3 takes bits 12-4
    ldh [FIT_rHDMA3], a
    ld a, l
    and $F0
    ldh [FIT_rHDMA4], a
    xor a
    ldh [FIT_rHDMA5], a ; one 16-byte block, general DMA
    ; map palette: $9800 + cy*32 + cx (VRAM bank 1)
    pop af
    push de
    ld e, a
    swap a
    and $0F
    ld l, a
    ld h, 0
    add hl, hl
    add hl, hl
    add hl, hl
    add hl, hl
    add hl, hl
    ld a, e
    and $0F
    or l
    ld l, a
    ld a, h
    or $98
    ld h, a
    ld a, c
    add LOW(nes_fit_stage_attr)
    ld e, a
    adc HIGH(nes_fit_stage_attr)
    sub e
    ld d, a
    ld a, $01
    ldh [rVBK], a
    ld a, [de]
    ld [hl], a
    pop de
    xor a
    ldh [rVBK], a
    ld a, e
    add 16
    ld e, a
    jr nc, .de_ok
    inc d
.de_ok:
    inc c
    dec b
    jr nz, .loop
    xor a
    ld [nes_fit_stage_n], a
    jr .out
.late:
    ; out of VBlank: the remaining slots go back to the dirty bitmap.
.relate:
    ld a, c
    add LOW(nes_fit_stage_slot)
    ld l, a
    adc HIGH(nes_fit_stage_slot)
    sub l
    ld h, a
    ld a, [hl]
    push bc
    call nes_fit_mark_slot
    pop bc
    inc c
    dec b
    jr nz, .relate
    xor a
    ld [nes_fit_stage_n], a
.out:
    xor a
    ldh [rVBK], a
    pop af
    ldh [rSVBK], a
    ret

; VBlank: choose the displayed page from the committed PPUCTRL/scroll (the
; playfield half of a captured raster split) and fix the letterbox scroll.
nes_fit_update_page:
    ldh a, [nes_split_active]
    and a
    jr z, .single
    ldh a, [nes_split_bottom_ctrl]
    ld b, a
    ldh a, [nes_split_bottom_x]
    ld d, a
    ldh a, [nes_split_bottom_y]
    ld e, a
    jr .have
.single:
    ld a, [nes_ppuctrl]
    ld b, a
    ld a, [nes_ppu_scroll_x]
    ld d, a
    ld a, [nes_ppu_scroll_y]
    ld e, a
.have:
    ld a, b
    and $03
    bit 7, d
    jr z, .nx
    xor $01
.nx:
    ld b, a
    ld a, e
    cp 120
    jr c, .ny
    cp 240
    jr nc, .ny
    ld a, b
    xor $02
    ld b, a
.ny:
    ld a, b
    add a
    add a
    add $20
    ld h, a
    ld l, 0
    ldh a, [rSVBK]
    push af
    call nes_ppu_map_nametable_hl
    pop af
    ldh [rSVBK], a
    ld a, h
    and $FC
    ld b, a
    ld a, [nes_fit_page]
    cp b
    jr z, .scroll
    ld a, b
    ld [nes_fit_page], a
    call nes_fit_mark_all
.scroll:
    ld a, $F0
    ldh [rSCX], a
    ld a, $F4
    ldh [rSCY], a
    ldh a, [rSTAT]
    and $BF
    ldh [rSTAT], a
    ret

; VBlank PPUCTRL commit: sprite size only; BG map $9800, signed tile ids.
nes_fit_update_ctrl:
    ldh a, [rLCDC]
    and $C3
    ld b, a
    ld a, [nes_ppuctrl]
    bit 5, a
    ld a, b
    jr z, .s
    or $04
.s:
    ldh [rLCDC], a
    ret

; VBlank ISR tail: GDMA the previous stage, then (after a commit) follow
; PPUCTRL/page and compose the next stage. Forces the FIT LCDC/scroll so no
; tip renderer path can leave its own view. Ends in WRAM bank 1.
nes_fit_frame_x:
    call nes_fit_flush
    ld a, [nes_fit_commit]
    and a
    jr z, .fix
    xor a
    ld [nes_fit_commit], a
    call nes_fit_update_ctrl
    call nes_fit_update_page
    call nes_fit_compose_budget
.fix:
    ldh a, [rLCDC]
    and $C7
    ldh [rLCDC], a
    ld a, $F0
    ldh [rSCX], a
    ld a, $F4
    ldh [rSCY], a
    ld a, $01
    ldh [rSVBK], a
    ret

SECTION "NES fit entry", ROM0
; ROM0 entry points into helper bank $FF; restore the code bank on return.
nes_fit_init:
    ld a, $FF
    ld [$2000], a
    call nes_fit_init_x
    jp nes_restore_code_bank

nes_fit_frame:
    ld a, $FF
    ld [$2000], a
    call nes_fit_frame_x
    jp nes_restore_code_bank
