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
; * Step 4 scroll ring: 17 pool columns x 15 rows = 255 slots follow the NES
;   horizontal scroll (SCX per 8 GB px + 16 px slots); the HUD split shows K
;   rows from $9C00 above an LYC split. Vertical scroll is still a page jump.

IF !DEF(NES2GBC_FIT_BUDGET)
DEF NES2GBC_FIT_BUDGET EQU 24
ENDC
IF !DEF(NES2GBC_FIT_CAP)
DEF NES2GBC_FIT_CAP EQU 8
ENDC
DEF FIT_rHDMA1 EQU $FF51
DEF FIT_rHDMA2 EQU $FF52
DEF FIT_rHDMA3 EQU $FF53
DEF FIT_rHDMA4 EQU $FF54
DEF FIT_rHDMA5 EQU $FF55
DEF FIT_CACHE_HI EQU $D8
DEF rWY EQU $FF4A
DEF rWX EQU $FF4B

SECTION "NES fit state", WRAM0
nes_fit_cell_dirty: ds 60   ; 2 physical nametables x 240 cells (2x2 NES tiles)
nes_fit_cell_any:   ds 1
nes_fit_bgpt:       ds 1    ; pattern table held by the cache (0/1, $FF = stale)
nes_fit_stage_n:    ds 1    ; composed slots waiting for VBlank
nes_fit_chr_any:    ds 1
nes_fit_commit:     ds 1    ; a completed NES frame was committed this VBlank
nes_fit_tiles:      ds 5    ; compose temp: 4 tile ids + palette
nes_fit_half_bank:  ds 2    ; CHR-ROM source of PT0/PT1 (ROM bank)
nes_fit_half_hi:    ds 2    ; ... and its address high byte ($40/$50)
nes_fit_tmp:        ds 8    ; one shrunk tile
; view: [ring page L, ring page R, hud rows K, hud w0, hud page L, hud page R]
nes_fit_stage_pos:  ds 1
nes_fit_ochg:       ds 1
nes_fit_osig:       ds 6    ; signature before this update (HUD compare)
nes_fit_ring_scx:   ds 1    ; SCX of the scrolled playfield
nes_fit_cur_s:      ds 1    ; compose temps
nes_fit_cur_page:   ds 1
nes_fit_cur_map:    ds 2
SECTION "NES fit chr dirty", WRAM0
nes_fit_chr_dirty:  ds 64   ; 512 CHR-RAM tile bits

; ring/view state: only touched with SVBK = 7
SECTION "NES fit view", WRAMX, BANK[7]
nes_fit_dirty:      ds 32   ; 255 pool-slot bits (slot = row*17 + pool column)
nes_fit_any:        ds 1    ; nonzero: some pool dirty bit may be set
nes_fit_valid:      ds 1    ; nonzero once the view (pages/columns) is known
nes_fit_sig:        ds 6
nes_fit_nsig:       ds 6
nes_fit_last_w0:    ds 1
nes_fit_w0:         ds 1    ; first visible ring world column (0-31)
nes_fit_col_pool:   ds 32   ; world column -> pool column ($FF = none)
nes_fit_owner:      ds 17   ; pool column -> world column ($FF = free)
nes_fit_blank:      ds 32   ; world map columns waiting to be blanked
nes_fit_blank_any:  ds 1

SECTION "NES fit stage", WRAMX[$D100], BANK[7]
nes_fit_stage:      ds NES2GBC_FIT_BUDGET * 16
nes_fit_stage_slot: ds NES2GBC_FIT_BUDGET
nes_fit_stage_attr: ds NES2GBC_FIT_BUDGET
nes_fit_stage_mlo:  ds NES2GBC_FIT_BUDGET
nes_fit_stage_mhi:  ds NES2GBC_FIT_BUDGET

SECTION "NES fit cache", WRAMX[$D800], BANK[7]
nes_fit_cache:      ds $800

SECTION "NES fit pack table", ROMX, BANK[255], ALIGN[8]
; bits 7,5,3,1 -> 7,6,5,4
nes_fit_pack_table:
FOR X, 256
    db ((X & $80) | ((X & $20) << 1) | ((X & $08) << 2) | ((X & $02) << 3)) & $F0
ENDR
; pool slot -> row ($FF: the blank slot 255) and pool column
nes_fit_srow:
FOR S, 256
IF S < 255
    db S / 17
ELSE
    db $FF
ENDC
ENDR
nes_fit_sp:
FOR S, 256
    db S % 17
ENDR
SECTION "NES fit bits", ROM0, ALIGN[3]
nes_fit_bit_table:
    db $01, $02, $04, $08, $10, $20, $40, $80

SECTION "NES fit init", ROMX, BANK[255]

; LCD off. Blank maps (tile $7F bank 1 = blank slot 255), cleared slot tiles,
; no columns assigned.
nes_fit_init_x:
    ; No extra call frame here: the ISR bank fix walks the stack.
    ld a, $07
    ldh [rSVBK], a
    ld a, $01
    ldh [rVBK], a
    ld hl, $9000
    ld bc, $0800
    call nes_video_fill_zero
    ld hl, $9800
    ld bc, $0800
.attr:
    ld a, $08
    ld [hli], a
    dec bc
    ld a, b
    or c
    jr nz, .attr
    xor a
    ldh [rVBK], a
    ld hl, $9000
    ld bc, $0800
    call nes_video_fill_zero
    ld hl, $9800
    ld bc, $0800
.ids:
    ld a, $7F
    ld [hli], a
    dec bc
    ld a, b
    or c
    jr nz, .ids
    ld a, $F0
    ldh [rSCX], a
    ld a, $F4
    ldh [rSCY], a
    ld a, 151
    ldh [rWX], a
    xor a
    ldh [rWY], a
    xor a
    ld [nes_fit_stage_n], a
    ld [nes_fit_stage_pos], a
    ld [nes_fit_chr_any], a
    ld [nes_fit_cell_any], a
    ld [nes_fit_blank_any], a
    ld [nes_fit_valid], a
    ld hl, nes_fit_chr_dirty
    ld b, 64
.clr:
    ld [hli], a
    dec b
    jr nz, .clr
    ld hl, nes_fit_cell_dirty
    ld b, 60
.clr2:
    ld [hli], a
    dec b
    jr nz, .clr2
    ld hl, nes_fit_blank
    ld b, 32
.clr3:
    ld [hli], a
    dec b
    jr nz, .clr3
    dec a
    ld hl, nes_fit_col_pool
    ld b, 32 + 17
.ff:
    ld [hli], a
    dec b
    jr nz, .ff
    ld [nes_fit_last_w0], a
    ld hl, nes_fit_sig
    ld b, 6
.ff2:
    ld [hli], a
    dec b
    jr nz, .ff2
    ld [nes_fit_bgpt], a
    ld a, $F0
    ld [nes_fit_ring_scx], a
    call nes_fit_mark_all
    ld a, $01
    ldh [rSVBK], a
    ret

SECTION "NES fit hot", ROM0
nes_fit_mark_all:
    ld hl, nes_fit_dirty
    ld a, $FF
    ld b, 32
.loop:
    ld [hli], a
    dec b
    jr nz, .loop
    ld [nes_fit_any], a
    ret

; A = pool slot (0-254). Clobbers A, BC, HL.
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

; A = cell (cy*16 + cx), D = nametable byte offset (0 / 30). Clobbers A, BC, HL.
nes_fit_mark_cell:
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
    add d
    add LOW(nes_fit_cell_dirty)
    ld l, a
    adc HIGH(nes_fit_cell_dirty)
    sub l
    ld h, a
    ld a, [hl]
    or b
    ld [hl], a
    ld a, $01
    ld [nes_fit_cell_any], a
    ret

; HL = physical nametable byte just changed ($D000-$D7FF). Preserves all but A/F.
nes_fit_nt_write:
    push bc
    push de
    push hl
    ld d, 0
    bit 2, h
    jr z, .nt0
    ld d, 30
.nt0:
    ld a, h
    and $03
    ld h, a
    cp $03
    jr nz, .tile
    ld a, l
    cp $C0
    jr nc, .attr
.tile:
    ; cell = h<<6 | (l>>6)<<4 | (l & $1E)>>1
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
    call nes_fit_mark_cell
.out:
    pop hl
    pop de
    pop bc
    ret
.attr:
    ; idx = l - $C0: ay = idx>>3, ax = idx&7; base cell = ay*32 + ax*2
    ld a, l
    sub $C0
    ld e, a
    and $07
    add a
    ld l, a
    ld a, e
    and $38
    add a
    add a
    or l
    ld e, a
    call nes_fit_mark_cell
    ld a, e
    inc a
    call nes_fit_mark_cell
    ld a, e
    cp $E0
    jr nc, .out         ; ay = 7: cell row 15 does not exist
    add 16
    call nes_fit_mark_cell
    ld a, e
    add 17
    call nes_fit_mark_cell
    jr .out
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

; Compose pool slot A into the stage at DE (16-byte aligned, SVBK any).
; Carry set: slot not shown (nothing staged). Else A = CGB attribute byte
; (palette + VRAM bank), DE advanced 16, nes_fit_cur_map = its map cell.
SECTION "NES fit compose", ROMX, BANK[255]
nes_fit_compose_slot:
    push de
    ld [nes_fit_cur_s], a
    ld l, a
    ld h, HIGH(nes_fit_srow)
    ld a, [hl]
    cp $FF
    jr z, .skip
    ld b, a               ; B = cy
    inc h
    ld c, [hl]            ; C = pool column
    ; map row base: cy*32
    ld a, b
    rrca
    rrca
    rrca
    and $E0
    ld l, a               ; L = (cy&7)<<5
    ld a, b
    rrca
    rrca
    rrca
    and $03
    ld h, a               ; H = cy>>3
    ld a, [nes_fit_sig + 2]
    ld e, a
    ld a, b
    cp e
    jr nc, .ring
    ; HUD row: pool column = HUD column (0-15), world w = hud_w0 + p
    ld a, c
    cp 16
    jr z, .skip
    add 2
    or l
    ld [nes_fit_cur_map], a
    ld a, h
    or $9C
    ld [nes_fit_cur_map + 1], a
    ld a, [nes_fit_sig + 3]
    add c
    and $1F
    ld c, a               ; C = w
    ld hl, nes_fit_sig + 4
    jr .page
.skip:
    pop de
    scf
    ret
.ring:
    ld a, c
    add LOW(nes_fit_owner)
    ld e, a
    adc HIGH(nes_fit_owner)
    sub e
    ld d, a
    ld a, [de]
    cp $FF
    jr z, .skip
    ld c, a               ; C = w
    or l
    ld [nes_fit_cur_map], a
    ld a, h
    or $98
    ld [nes_fit_cur_map + 1], a
    ld hl, nes_fit_sig
.page:
    bit 4, c
    jr z, .pl
    inc hl
.pl:
    ld a, [hl]
    ld [nes_fit_cur_page], a
    ld a, c
    and $0F
    ld c, a               ; C = cx, B = cy
    pop de                ; stage pointer
    ld a, $01
    ldh [rSVBK], a
    ; NT address = page + (cy>>2) : (cy&3)<<6 | cx*2
    ld a, b
    rrca
    rrca
    and $03
    ld h, a
    ld a, [nes_fit_cur_page]
    add h
    ld h, a
    ld a, b
    and $03
    rrca
    rrca
    ld l, a
    ld a, c
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
    ld a, [nes_fit_cur_page]
    add $03
    ld h, a
    ld a, b
    and $0E
    add a
    add a
    ld l, a
    ld a, c
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
    ld b, a
    ld a, [nes_fit_cur_s]
    bit 7, a
    ld a, b
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
    and a                 ; carry clear
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

; Dirty nametable cells -> dirty pool slots of every view position showing them.
nes_fit_translate:
    xor a
    ld [nes_fit_cell_any], a
    ld c, 0
.byte:
    ld a, c
    add LOW(nes_fit_cell_dirty)
    ld l, a
    adc HIGH(nes_fit_cell_dirty)
    sub l
    ld h, a
    ld a, [hl]
    and a
    jr z, .next_byte
    ld [hl], 0
    ld b, 0
.bit:
    srl a
    jr nc, .next_bit
    push af
    push bc
    call .cell
    pop bc
    pop af
.next_bit:
    inc b
    and a
    jr nz, .bit
.next_byte:
    inc c
    ld a, c
    cp 60
    jr nz, .byte
    ret
.cell:
    ; C = byte (0-59), B = bit -> D = physical page, E = cell
    ld d, $D0
    ld a, c
    cp 30
    jr c, .lo
    sub 30
    ld d, $D4
.lo:
    add a
    add a
    add a
    or b
    ld e, a
    swap a
    and $0F
    ld b, a               ; B = cy
    ld a, [nes_fit_sig + 2]
    ld c, a
    ld a, b
    cp c
    jr nc, .ring
    ; HUD rows
    ld hl, nes_fit_sig + 4
    ld c, 0
    call .hud_half
    ld hl, nes_fit_sig + 5
    ld c, 16
.hud_half:
    ld a, [hl]
    cp d
    ret nz
    ld a, e
    and $0F
    or c
    ld c, a
    ld a, [nes_fit_sig + 3]
    cpl
    inc a
    add c
    and $1F
    cp 16
    ret nc
    jr .mark
.ring:
    ld hl, nes_fit_sig
    ld c, 0
    call .ring_half
    ld hl, nes_fit_sig + 1
    ld c, 16
.ring_half:
    ld a, [hl]
    cp d
    ret nz
    ld a, e
    and $0F
    or c
    add LOW(nes_fit_col_pool)
    ld l, a
    adc HIGH(nes_fit_col_pool)
    sub l
    ld h, a
    ld a, [hl]
    cp $FF
    ret z
.mark:
    ; A = pool column, B = cy: slot = cy*17 + p
    ld c, a
    ld a, b
    swap a
    add b
    add c
    push bc
    push de
    call nes_fit_mark_slot
    pop de
    pop bc
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
    ld a, $07
    ldh [rSVBK], a
    ld a, [nes_fit_stage_n]
    and a
    jp nz, .out
    ld a, [nes_fit_valid]
    and a
    jp z, .out
    ld a, [nes_fit_cell_any]
    and a
    call nz, nes_fit_translate
    ld a, [nes_fit_any]
    and a
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
    call nes_fit_compose_slot
    jr c, .skipped
    ld b, a              ; attr
    ld a, [nes_fit_stage_n]
    ld c, a
    ld hl, nes_fit_stage_attr
    call .idx
    ld [hl], b
    ld hl, nes_fit_stage_slot
    call .idx
    ld a, [nes_fit_cur_s]
    ld [hl], a
    ld hl, nes_fit_stage_mlo
    call .idx
    ld a, [nes_fit_cur_map]
    ld [hl], a
    ld hl, nes_fit_stage_mhi
    call .idx
    ld a, [nes_fit_cur_map + 1]
    ld [hl], a
    ld a, c
    inc a
    ld [nes_fit_stage_n], a
    pop hl
    pop bc
    cp NES2GBC_FIT_CAP
    jr z, .full
    pop af
    jr .next_bit
.skipped:
    pop hl
    pop bc
    pop af
.next_bit:
    inc b
    and a
    jr nz, .bit
.next_byte:
    inc c
    ld a, c
    cp 32
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
.idx:
    ; HL += C
    ld a, l
    add c
    ld l, a
    ret nc
    inc h
    ret

; VBlank: blank released map columns, then GDMA every staged slot into VRAM
; and write its map cell (tile id + attribute).
nes_fit_flush:
    ldh a, [rSVBK]
    push af
    ld a, $07
    ldh [rSVBK], a
    ld a, [nes_fit_blank_any]
    and a
    call nz, nes_fit_blank_cols
    ld a, [nes_fit_stage_n]
    and a
    jp z, .out
    ld b, a
    ld a, [nes_fit_stage_pos]
    ld c, a
    ld l, a
    ld h, 0
    add hl, hl
    add hl, hl
    add hl, hl
    add hl, hl
    ld de, nes_fit_stage
    add hl, de
    ld d, h
    ld e, l
    ld a, b
    sub c
    ld b, a
.loop:
    ldh a, [rLY]
    cp 144
    jr c, .late
    ld hl, nes_fit_stage_slot
    call nes_fit_compose_budget.idx
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
    pop af
    push de
    push af
    ld hl, nes_fit_stage_mlo
    call nes_fit_compose_budget.idx
    ld e, [hl]
    ld hl, nes_fit_stage_mhi
    call nes_fit_compose_budget.idx
    ld d, [hl]
    ld hl, nes_fit_stage_attr
    call nes_fit_compose_budget.idx
    xor a
    ldh [rVBK], a
    pop af
    and $7F
    ld [de], a
    ld a, $01
    ldh [rVBK], a
    ld a, [hl]
    ld [de], a
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
    ld [nes_fit_stage_pos], a
    jr .out
.late:
    ; out of VBlank: keep the rest staged for the next VBlank.
    ld a, c
    ld [nes_fit_stage_pos], a
.out:
    xor a
    ldh [rVBK], a
    pop af
    ldh [rSVBK], a
    ret

; VBlank: write blank (tile $7F, bank 1) into released ring map columns.
nes_fit_blank_cols:
    xor a
    ld [nes_fit_blank_any], a
    ld c, 0
.col:
    ld a, c
    add LOW(nes_fit_blank)
    ld l, a
    adc HIGH(nes_fit_blank)
    sub l
    ld h, a
    ld a, [hl]
    and a
    jr z, .next
    ldh a, [rLY]
    cp 144
    jr c, .late
    ld [hl], 0
    ld b, 15
    ld hl, $9800
    ld a, l
    or c
    ld l, a
.row:
    xor a
    ldh [rVBK], a
    ld [hl], $7F
    inc a
    ldh [rVBK], a
    ld [hl], $08
    ld a, l
    add 32
    ld l, a
    jr nc, .nc
    inc h
.nc:
    dec b
    jr nz, .row
.next:
    inc c
    ld a, c
    cp 32
    jr nz, .col
    xor a
    ldh [rVBK], a
    ret
.late:
    ld a, $01
    ld [nes_fit_blank_any], a
    xor a
    ldh [rVBK], a
    ret

; A = logical nametable 0-3 -> A = physical page high byte ($D0/$D4).
nes_fit_map_nt:
    ; same mapping as nes_ppu_map_nametable_hl (mirroring 1 = vertical,
    ; 3/4 = one-screen low/high, else horizontal), no WRAM bank switch
    and $03
    ld l, a
    ld a, [nes_mirroring]
    cp 5
    jr c, .m
    xor a
.m:
    add a
    add a
    add l
    add LOW(nes_fit_mir_table)
    ld l, a
    adc HIGH(nes_fit_mir_table)
    sub l
    ld h, a
    ld a, [hl]
    ret
nes_fit_mir_table:
    db $D0, $D0, $D4, $D4
    db $D0, $D4, $D0, $D4
    db $D0, $D0, $D4, $D4
    db $D0, $D0, $D0, $D0
    db $D4, $D4, $D4, $D4

; VBlank after a commit: playfield scroll -> ring columns / SCX, HUD split ->
; K rows, and the page signature (any change recomposes everything).
nes_fit_update_view:
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
    ; w0 = (ctrl&1)<<4 | x>>4 ; ring_scx = ((ctrl&1)<<7 | x>>1) - 16
    ld a, d
    swap a
    and $0F
    bit 0, b
    jr z, .w
    or $10
.w:
    ld [nes_fit_w0], a
    ld a, d
    srl a
    bit 0, b
    jr z, .s
    or $80
.s:
    add $F0
    ld [nes_fit_ring_scx], a
    ; vertical page select
    ld a, b
    and $02
    ld c, a
    ld a, e
    cp 120
    jr c, .ny
    cp 240
    jr nc, .ny
    ld a, c
    xor $02
    ld c, a
.ny:
    ld a, c
    call nes_fit_map_nt
    ld [nes_fit_nsig + 0], a
    ld a, c
    inc a
    call nes_fit_map_nt
    ld [nes_fit_nsig + 1], a
    ; HUD
    xor a
    ld [nes_fit_nsig + 2], a
    ld [nes_fit_nsig + 3], a
    ld [nes_fit_nsig + 4], a
    ld [nes_fit_nsig + 5], a
    ldh a, [nes_split_active]
    and a
    jr z, .cmp
    ld a, [nes_oam_ram]
    add 23
    jr c, .k7
    swap a
    and $0F
    jr nz, .k1
    inc a
.k1:
    cp 8
    jr c, .kok
.k7:
    ld a, 7
.kok:
    ld [nes_fit_nsig + 2], a
    ldh a, [nes_split_top_ctrl]
    ld b, a
    ldh a, [nes_split_top_x]
    swap a
    and $0F
    bit 0, b
    jr z, .hw
    or $10
.hw:
    ld [nes_fit_nsig + 3], a
    ld a, b
    and $02
    ld c, a
    call nes_fit_map_nt
    ld [nes_fit_nsig + 4], a
    ld a, c
    inc a
    call nes_fit_map_nt
    ld [nes_fit_nsig + 5], a
.cmp:
    ld hl, nes_fit_sig
    ld de, nes_fit_osig
    ld b, 6
.oc:
    ld a, [hli]
    ld [de], a
    inc de
    dec b
    jr nz, .oc
    ld hl, nes_fit_nsig
    ld de, nes_fit_sig
    ld b, 6
    ld c, 0
    xor a
    ld [nes_fit_ochg], a
.cl:
    ld a, [de]
    cp [hl]
    jr z, .same
    ld c, 1
    ld a, [hl]
    ld [de], a
    ld a, b
    cp 4                  ; b = 6,5,4 -> sig[0..2]
    jr c, .same
    ld a, 1
    ld [nes_fit_ochg], a
.same:
    inc hl
    inc de
    dec b
    jr nz, .cl
    ld a, c
    and a
    call nz, nes_fit_update_view.changed
    ld a, 1
    ld [nes_fit_valid], a
    ; columns only move when w0 does (mark_all keeps the assignment)
    ld a, [nes_fit_w0]
    ld hl, nes_fit_last_w0
    cp [hl]
    ret z
    ld [hl], a
    ; fall through: column assignment

; Release pool columns that left w0..w0+16, give every visible world column a
; pool column (fresh ones are marked for ring rows K..14).
nes_fit_assign:
    ld c, 0
.rel:
    ld a, c
    add LOW(nes_fit_owner)
    ld l, a
    adc HIGH(nes_fit_owner)
    sub l
    ld h, a
    ld a, [hl]
    cp $FF
    jr z, .rnext
    ld b, a
    ld a, [nes_fit_w0]
    cpl
    inc a
    add b
    and $1F
    cp 17
    jr c, .rnext
    ld [hl], $FF
    ld a, b
    add LOW(nes_fit_col_pool)
    ld l, a
    adc HIGH(nes_fit_col_pool)
    sub l
    ld h, a
    ld [hl], $FF
    ld de, nes_fit_blank - nes_fit_col_pool
    add hl, de
    ld [hl], 1
    ld a, 1
    ld [nes_fit_blank_any], a
.rnext:
    inc c
    ld a, c
    cp 17
    jr nz, .rel
    ld a, [nes_fit_w0]
    ld c, a
    ld b, 17
.vis:
    push bc
    ld a, c
    and $1F
    ld c, a                ; C = w
    add LOW(nes_fit_col_pool)
    ld l, a
    adc HIGH(nes_fit_col_pool)
    sub l
    ld h, a
    ld a, [hl]
    cp $FF
    jr nz, .vnext
    ; find a free pool column
    push hl
    ld hl, nes_fit_owner
    ld e, 0
.find:
    ld a, [hl]
    cp $FF
    jr z, .got
    inc hl
    inc e
    ld a, e
    cp 17
    jr nz, .find
    pop hl
    jr .vnext             ; cannot happen
.got:
    ld [hl], c
    pop hl
    ld [hl], e
    ld de, nes_fit_blank - nes_fit_col_pool
    push hl
    add hl, de
    ld [hl], 0
    pop hl
    ld a, [hl]
    ld e, a               ; E = p
    ; mark rows K..14: slot = row*17 + p
    ld a, [nes_fit_sig + 2]
    ld d, a
.mk:
    ld a, d
    cp 15
    jr nc, .vnext
    swap a
    add d
    add e
    push de
    call nes_fit_mark_slot
    pop de
    inc d
    jr .mk
.vnext:
    pop bc
    inc c
    dec b
    jr nz, .vis
    ret

nes_fit_update_view.changed:
    ; rp0/rp1/K changed -> everything; only HUD fields -> HUD rows 0..K-1
    ld a, [nes_fit_ochg]
    and a
    jp nz, nes_fit_mark_all
    ld a, [nes_fit_sig + 2]
    and a
    ret z
    call nes_fit_hud_same
    ret z
    ld d, 0
.hr:
    ld e, 0
.hc:
    ld a, d
    swap a
    add d
    add e
    push de
    call nes_fit_mark_slot
    pop de
    inc e
    ld a, e
    cp 17
    jr nz, .hc
    inc d
    ld a, [nes_fit_sig + 2]
    cp d
    jr nz, .hr
    ret

; HUD fields changed but the HUD may show the same bytes (SMB keeps its
; status bar in both nametables and flips the HUD page as the world scrolls).
; Both views page-aligned (x>>4 = 0): compare the K visible cell rows and
; their attributes. Z = same (nothing to redraw).
nes_fit_hud_same:
    ld a, [nes_fit_osig + 3]
    ld b, a
    ld a, [nes_fit_sig + 3]
    or b
    and $0F
    ret nz
    ld hl, nes_fit_osig + 4
    bit 4, b
    jr z, .o
    inc hl
.o:
    ld d, [hl]
    ld hl, nes_fit_sig + 4
    ld a, [nes_fit_sig + 3]
    bit 4, a
    jr z, .n
    inc hl
.n:
    ld e, [hl]
    ld a, d
    cp e
    ret z
    ; tiles: K cell rows = 2K NES rows = K*64 bytes from each page start
    ld a, [nes_fit_sig + 2]
    ld c, a
    ld a, $01
    ldh [rSVBK], a
    ld h, d
    ld l, 0
    ld d, e
    ld e, 0
.row:
    ld b, 64
.t:
    ld a, [de]
    cp [hl]
    jr nz, .diff
    inc l
    inc e
    dec b
    jr nz, .t
    ld a, l
    and a
    jr nz, .t2
    inc h
    inc d
.t2:
    dec c
    jr nz, .row
    ; attributes: ceil(K/2) rows of 8 at +$3C0
    ld a, h
    and $FC
    add $03
    ld h, a
    ld a, d
    and $FC
    add $03
    ld d, a
    ld l, $C0
    ld e, l
    ld a, [nes_fit_sig + 2]
    inc a
    srl a
    add a
    add a
    add a
    ld b, a
.a:
    ld a, [de]
    cp [hl]
    jr nz, .diff
    inc l
    inc e
    dec b
    jr nz, .a
    ld a, $07
    ldh [rSVBK], a
    xor a                 ; Z
    ret
.diff:
    ld a, $07
    ldh [rSVBK], a
    or $FF                ; NZ
    ret

; VBlank PPUCTRL commit: sprite size only.
nes_fit_update_ctrl:
    ldh a, [rLCDC]
    and $FB
    ld b, a
    ld a, [nes_ppuctrl]
    bit 5, a
    ld a, b
    jr z, .s
    or $04
.s:
    ldh [rLCDC], a
    ret

; Force the FIT display: window (blank right gutter), HUD map above LYC,
; scrolled ring below it.
nes_fit_display:
    ld a, 151
    ldh [rWX], a
    xor a
    ldh [rWY], a
    ld a, $F4
    ldh [rSCY], a
    ldh a, [rLCDC]
    and $87
    or $60
    ld b, a
    ld a, [nes_fit_sig + 2]
    and a
    jr z, .ring
    add a
    add a
    add a
    add 11
    ld c, a
    ldh a, [rLY]
    cp 144
    jr nc, .hud_ok
    add 2
    cp c
    jr nc, .ring          ; past the split line: no HUD split this frame
.hud_ok:
    ld a, c
    ldh [rLYC], a
    ld a, b
    or $08
    ldh [rLCDC], a
    xor a
    ldh [rSCX], a
    ldh a, [rSTAT]
    or $40
    ldh [rSTAT], a
    ret
.ring:
    ld a, b
    ldh [rLCDC], a
    ld a, [nes_fit_ring_scx]
    ldh [rSCX], a
    ldh a, [rSTAT]
    and $BF
    ldh [rSTAT], a
    ret

; VBlank ISR tail. Ends in WRAM bank 1.
nes_fit_frame_x:
    call nes_fit_flush
    ld a, [nes_fit_commit]
    and a
    jr z, .fix
    call nes_fit_update_ctrl
    ld a, $07
    ldh [rSVBK], a
    call nes_fit_update_view
.fix:
    ld a, $07
    ldh [rSVBK], a
    call nes_fit_display
    ld a, [nes_fit_commit]
    and a
    jr z, .done
    xor a
    ld [nes_fit_commit], a
    ; let the HUD-split STAT interrupt nest while composing
    ld a, $02
    ldh [rIE], a
    ei
    call nes_fit_compose_budget
    di
    ld a, $03
    ldh [rIE], a
.done:
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

nes_fit_flush_early:
    ld a, $FF
    ld [$2000], a
    call nes_fit_flush
    jp nes_restore_code_bank

nes_fit_frame:
    ld a, $FF
    ld [$2000], a
    call nes_fit_frame_x
    jp nes_restore_code_bank

