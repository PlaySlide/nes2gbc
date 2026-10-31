; FIT_SCREEN + FIT_WIDE (opt-in, make generate FIT_SCREEN=1 FIT_WIDE=1): show
; the whole NES 256x240 screen as 160x120 on the CGB (X x5/8, Y x1/2). Same
; nes_fit_* entry points as fit.asm (main.asm includes one or the other).
;
; * Every NES tile is shrunk to a 5x4 crumb (top-left of an 8x8 CGB tile):
;   CHR-ROM at build time (assets::fit_shrink_tile_wide), CHR-RAM here with
;   the same rule (rows a over b; column bins {0,1}{2,3}{4}{5,6}{7}, the left
;   pixel winning when opaque).
; * Sprites: only the active sprite pattern table is held, tiles 0-127 at VRAM
;   bank 0 $8000 and 128-255 at bank 1 $8000 (OAM bank = tile bit 7).
; * BG: one CGB tile = one 8x8 output cell = 1.6 x 2 NES tiles. Output column
;   w (0-39 across both nametables) takes NES tile columns q, q+1, q+2 at
;   phase p (q = 8w/5, p = 8w mod 5); its palette comes from the tile covering
;   most of it. 480 slots (row*32 + map column) at $8800-$97FF in both banks
;   (signed ids, bank = row >= 8): the maps at $9800 / $9C00 are static except
;   the palette.
; * Scroll ring: a direct-mapped 32-column map. The scaled scroll X advances a
;   virtual ring position R (SCX); visible map column (R/8 + i) holds world
;   column (w0 + i) mod 40; a column changing owner is recomposed (rows K..14).
;   The HUD split shows K rows from $9C00 above an LYC split. No window.

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
nes_fit_spt:        ds 1    ; sprite pattern table held at $8000 (0/1)
nes_fit_spr_pend:   ds 1    ; CHR-ROM sprite table GDMA quarters still to copy
nes_fit_stage_n:    ds 1    ; composed slots waiting for VBlank
nes_fit_chr_any:    ds 1
nes_fit_commit:     ds 1    ; a completed NES frame was committed this VBlank
nes_fit_tiles:      ds 7    ; compose temp: 3 top + 3 bottom tile ids, attr
nes_fit_half_bank:  ds 2    ; CHR-ROM source of PT0/PT1 (ROM bank)
nes_fit_half_hi:    ds 2    ; ... and its address high byte ($40/$50)
nes_fit_tmp:        ds 8    ; one shrunk tile
; view: [ring page L, ring page R, hud rows K, hud w0 (0-39), hud page L, hud page R]
nes_fit_stage_pos:  ds 1
nes_fit_ochg:       ds 1
nes_fit_osig:       ds 6    ; signature before this update (HUD compare)
nes_fit_ring_scx:   ds 1    ; SCX of the scrolled playfield (= ring position R)
nes_fit_cur_s:      ds 1    ; compose temps: slot low byte
nes_fit_cur_map:    ds 2
nes_fit_pg:         ds 2
nes_fit_rowh:       ds 1
nes_fit_rowl:       ds 1
nes_fit_q:          ds 1
nes_fit_p:          ds 1
nes_fit_stp:        ds 2
SECTION "NES fit chr dirty", WRAM0
nes_fit_chr_dirty:  ds 64   ; 512 CHR-RAM tile bits

; ring/view state: only touched with SVBK = 7
SECTION "NES fit view", WRAMX, BANK[7]
nes_fit_any:        ds 1    ; nonzero: some slot dirty bit may be set
nes_fit_valid:      ds 1    ; nonzero once the view (pages/columns) is known
nes_fit_sig:        ds 6
nes_fit_nsig:       ds 6
nes_fit_w0:         ds 1    ; first visible world column (0-39)
nes_fit_last_w0:    ds 1
nes_fit_last_mc:    ds 1
nes_fit_lastx:      ds 2    ; scaled view X (0-319) at the last update
SECTION "NES fit ring tabs", WRAMX, BANK[7], ALIGN[8]
nes_fit_owner:      ds 32   ; map column -> world column ($FF = none)
nes_fit_newown:     ds 32
nes_fit_wmap:       ds 40   ; world column -> map column ($FF = none)
nes_fit_dirty:      ds 60   ; 480 slot bits (byte = row*4 + col/8)

SECTION "NES fit stage", WRAMX[$D100], BANK[7]
nes_fit_stage:      ds NES2GBC_FIT_BUDGET * 16
nes_fit_stage_slot: ds NES2GBC_FIT_BUDGET
nes_fit_stage_attr: ds NES2GBC_FIT_BUDGET
nes_fit_stage_mlo:  ds NES2GBC_FIT_BUDGET
nes_fit_stage_mhi:  ds NES2GBC_FIT_BUDGET

SECTION "NES fit cache", WRAMX[$D800], BANK[7]
nes_fit_cache:      ds $800

SECTION "NES fit wide tables", ROMX, BANK[255], ALIGN[8]
; row opacity n -> kept pixels (one per bin; the left one when opaque)
nes_fitw_msk:
FOR N, 256
    db $09 | ((N & $80) | ((~N & $80) >> 1)) | ((N & $20) | ((~N & $20) >> 1)) | ((N & $04) | ((~N & $04) >> 1))
ENDR
; masked row -> bins in bits 7..3
nes_fitw_pack:
FOR X, 256
    db ((X & $C0) != 0) * $80 | ((X & $30) != 0) * $40 | ((X & $08) != 0) * $20 | ((X & $06) != 0) * $10 | ((X & $01) != 0) * $08
ENDR
; world column -> q = 8w/5, then p = 8w mod 5
nes_fitw_q:
FOR W, 40
    db (8 * W) / 5
ENDR
FOR W, 40
    db (8 * W) % 5
ENDR
; NES cell column cx' (0-31) -> first world column it touches (10cx'/8)
nes_fitw_c2w:
FOR CI, 32
    db (10 * CI) / 8
ENDR
SECTION "NES fit bits", ROM0, ALIGN[3]
nes_fit_bit_table:
    db $01, $02, $04, $08, $10, $20, $40, $80

SECTION "NES fit init", ROMX, BANK[255]

; LCD off. Static maps (slot ids / bank, blank tile $60 bank 1 outside the 15
; rows), cleared BG slot tiles, no columns assigned.
nes_fit_init_x:
    ; No extra call frame here: the ISR bank fix walks the stack.
    ld a, $07
    ldh [rSVBK], a
    ld a, $01
    ldh [rVBK], a
    ld hl, $8800
    ld bc, $1000
    call nes_video_fill_zero
    xor a
    ldh [rVBK], a
    ld hl, $8800
    ld bc, $1000
    call nes_video_fill_zero
    ld hl, $9800
    ld d, 0
.r:
    ld e, 0
.m:
    ld a, d
    and $1F
    cp 15
    jr nc, .blank
    ld a, d
    and $07
    rrca
    rrca
    rrca
    or e
    xor $80
    ld b, a
    ld a, d
    and $1F
    cp 8
    ld c, 0
    jr c, .w
    ld c, $08
    jr .w
.blank:
    ld b, $60
    ld c, $08
.w:
    xor a
    ldh [rVBK], a
    ld [hl], b
    inc a
    ldh [rVBK], a
    ld [hl], c
    inc hl
    inc e
    ld a, e
    cp 32
    jr nz, .m
    inc d
    ld a, d
    cp 64
    jr nz, .r
    xor a
    ldh [rVBK], a
    ldh [rSCX], a
    ld a, $F4
    ldh [rSCY], a
    ld a, 167
    ldh [rWX], a
    xor a
    ldh [rWY], a
    ld [nes_fit_stage_n], a
    ld [nes_fit_stage_pos], a
    ld [nes_fit_chr_any], a
    ld [nes_fit_cell_any], a
    ld [nes_fit_valid], a
    ld [nes_fit_spt], a
    ld [nes_fit_spr_pend], a
    ld [nes_fit_ring_scx], a
    ld [nes_fit_lastx], a
    ld [nes_fit_lastx + 1], a
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
    dec a
    ld hl, nes_fit_owner
    ld b, 32 + 32 + 40
.ff:
    ld [hli], a
    dec b
    jr nz, .ff
    ld [nes_fit_last_w0], a
    ld [nes_fit_last_mc], a
    ld hl, nes_fit_sig
    ld b, 6
.ff2:
    ld [hli], a
    dec b
    jr nz, .ff2
    ld [nes_fit_bgpt], a
    call nes_fit_mark_all
    ld a, $01
    ldh [rSVBK], a
    ret

SECTION "NES fit hot", ROM0
nes_fit_mark_all:
    ld hl, nes_fit_dirty
    ld a, $FF
    ld b, 60
.loop:
    ld [hli], a
    dec b
    jr nz, .loop
    ld [nes_fit_any], a
    ret

; B = row (0-14), A = map column (0-31). Clobbers A, C, HL; keeps B, DE.
nes_fit_mark_rc:
    ld c, a
    ld a, b
    add a
    add a
    ld l, a
    ld a, c
    rrca
    rrca
    rrca
    and $03
    add l
    add LOW(nes_fit_dirty)
    ld l, a
    ld h, HIGH(nes_fit_dirty)
    ld a, c
    and $07
    add LOW(nes_fit_bit_table)
    ld c, a
    push bc
    ld b, HIGH(nes_fit_bit_table)
    ld a, [bc]
    pop bc
    or [hl]
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

; Shrink one raw NES CHR-RAM tile to a 5x4 crumb. A = tile (low 8 bits),
; B = pattern table. Output nes_fit_tmp[0..7]. Leaves SVBK changed.
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
    or b                 ; n = lo | hi
    push hl
    ld l, a
    ld h, HIGH(nes_fitw_msk)
    ld a, [hl]           ; kept pixels
    ld l, a
    and c
    ld c, l              ; C = mask
    ld l, a
    ld h, HIGH(nes_fitw_pack)
    ld a, [hl]
    ld [de], a
    inc de
    ld a, b
    and c
    ld l, a
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
; The sprite table goes to VRAM bank E>>7, $8000 + (E&$7F)*16; the BG cache
; is updated (and all slots dirtied) when it holds table D.
nes_fit_store_tile:
    ld a, [nes_fit_spt]
    cp d
    jr nz, .cache
    ld a, e
    and $7F
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
    ld a, e
    rlca
    and $01
    ld c, a
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
.cache:
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

SECTION "NES fitw r0 nes_fit_note_half", ROM0
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

; FIT_WIDE: copy the sprite pattern table (noted CHR-ROM half nes_fit_spt) to
; VRAM bank 0 $8000 (tiles 0-127) and bank 1 $8000 (128-255). LCD off or a
; context that already allowed the 4 KiB copy. Leaves VBK 0, the CHR bank in.
SECTION "NES fitw r0 nes_fit_sprite_copy", ROM0
nes_fit_sprite_copy:
    ld a, [nes_fit_spt]
    and a
    ld a, [nes_fit_half_bank]
    ld b, a
    ld a, [nes_fit_half_hi]
    jr z, .s0
    ld a, [nes_fit_half_bank + 1]
    ld b, a
    ld a, [nes_fit_half_hi + 1]
.s0:
    ld h, a
    ld l, 0
    ld a, b
    ld [$2000], a
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

; A = CHR half just re-noted (MMC1 4 KiB path): recopy it if it is the
; sprite table.
SECTION "NES fitw r0 nes_fit_half_changed", ROM0
nes_fit_half_changed:
    ld b, a
    ld a, [nes_fit_spt]
    cp b
    ret nz
    jp nes_fit_sprite_copy

; VBlank, CHR-ROM sprite table switch: one 1 KiB quarter per frame by GDMA
; (A = quarters left, 4..1).
SECTION "NES fitw r0 nes_fit_spr_gdma", ROM0
nes_fit_spr_gdma:
    ldh a, [rLY]
    cp 144
    ret c
    ld a, [nes_fit_spr_pend]
    dec a
    ld [nes_fit_spr_pend], a
    xor $03               ; quarter 0..3
    ld c, a
    ld a, [nes_fit_spt]
    and a
    ld a, [nes_fit_half_bank]
    ld b, a
    ld a, [nes_fit_half_hi]
    jr z, .s0
    ld a, [nes_fit_half_bank + 1]
    ld b, a
    ld a, [nes_fit_half_hi + 1]
.s0:
    ld h, a
    ld a, b
    ld [$2000], a
    ld a, c
    add a
    add a
    add h
    ldh [FIT_rHDMA1], a   ; src = half + quarter*$400
    xor a
    ldh [FIT_rHDMA2], a
    ld a, c
    srl a
    ldh [rVBK], a         ; quarters 0,1 -> bank 0; 2,3 -> bank 1
    ld a, c
    and $01
    add a
    add a
    ldh [FIT_rHDMA3], a   ; dst $8000 / $8400
    xor a
    ldh [FIT_rHDMA4], a
    ld a, $3F
    ldh [FIT_rHDMA5], a   ; 64 blocks, general DMA
    xor a
    ldh [rVBK], a
    ret

; Compose slot (B = row 0-14, C = map column 0-31) into the stage at DE
; (16-byte aligned). Carry set: not shown. Else A = CGB attribute (palette +
; VRAM bank), DE advanced 16, nes_fit_cur_map / nes_fit_cur_s set.
SECTION "NES fit compose", ROMX, BANK[255]
nes_fit_compose_slot:
    push de
    ld a, b
    rrca
    rrca
    rrca
    ld h, a
    and $E0
    or c
    ld [nes_fit_cur_s], a
    ld [nes_fit_cur_map], a
    ld a, h
    and $03
    ld h, a               ; H = row >> 3
    ld a, [nes_fit_sig + 2]
    ld e, a
    ld a, b
    cp e
    jr nc, .ring
    ; HUD row: map column = HUD column (0-20), w = hud w0 + c (mod 40)
    ld a, c
    cp 21
    jr nc, .skip
    ld a, h
    or $9C
    ld [nes_fit_cur_map + 1], a
    ld a, [nes_fit_sig + 3]
    add c
    cp 40
    jr c, .hw
    sub 40
.hw:
    ld c, a
    ld hl, nes_fit_sig + 4
    jr .page
.skip:
    pop de
    scf
    ret
.ring:
    ld a, h
    or $98
    ld [nes_fit_cur_map + 1], a
    ld a, c
    add LOW(nes_fit_owner)
    ld l, a
    ld h, HIGH(nes_fit_owner)
    ld a, [hl]
    cp $FF
    jr z, .skip
    ld c, a
    ld hl, nes_fit_sig
.page:
    ; C = w, B = row, HL = page pair
    ld a, [hli]
    ld [nes_fit_pg], a
    ld a, [hl]
    ld [nes_fit_pg + 1], a
    ld a, b
    rrca
    rrca
    ld e, a
    and $03
    ld [nes_fit_rowh], a
    ld a, e
    and $C0
    ld [nes_fit_rowl], a
    ld a, c
    add LOW(nes_fitw_q)
    ld l, a
    adc HIGH(nes_fitw_q)
    sub l
    ld h, a
    ld a, [hl]
    ld [nes_fit_q], a
    ld de, 40
    add hl, de
    ld a, [hl]
    ld [nes_fit_p], a
    ld a, $01
    ldh [rSVBK], a
    ld a, [nes_fit_q]
    call .nta
    ld a, [hl]
    ld [nes_fit_tiles + 0], a
    set 5, l
    ld a, [hl]
    ld [nes_fit_tiles + 3], a
    ld a, [nes_fit_q]
    inc a
    call .nta
    ld a, [hl]
    ld [nes_fit_tiles + 1], a
    set 5, l
    ld a, [hl]
    ld [nes_fit_tiles + 4], a
    ld a, [nes_fit_p]
    cp 3
    jr c, .no2
    ld a, [nes_fit_q]
    add 2
    call .nta
    ld a, [hl]
    ld [nes_fit_tiles + 2], a
    set 5, l
    ld a, [hl]
    ld [nes_fit_tiles + 5], a
.no2:
    ; palette: tile q (p < 2) or q+1 covers most of the cell
    ld a, [nes_fit_p]
    cp 2
    ld a, [nes_fit_q]
    jr c, .pc
    inc a
.pc:
    and $3F
    ld c, a
    bit 5, a
    ld a, [nes_fit_pg]
    jr z, .pa
    ld a, [nes_fit_pg + 1]
.pa:
    add $03
    ld h, a
    ld a, b
    and $0E
    add a
    add a
    ld l, a
    ld a, c
    and $1F
    rrca
    rrca
    and $07
    or l
    or $C0
    ld l, a
    ld a, [hl]
    bit 0, b
    jr z, .no_ys
    swap a
.no_ys:
    bit 1, c
    jr z, .no_xs
    rrca
    rrca
.no_xs:
    and $03
    ld c, a
    ld a, b
    cp 8
    ld a, c
    jr c, .bank0
    or $08
.bank0:
    ld [nes_fit_tiles + 6], a
    ld a, $07
    ldh [rSVBK], a
    pop hl
    ld a, l
    ld [nes_fit_stp], a
    ld a, h
    ld [nes_fit_stp + 1], a
    ld a, [nes_fit_p]
    add a
    add LOW(.jt)
    ld l, a
    adc HIGH(.jt)
    sub l
    ld h, a
    ld a, [hli]
    ld h, [hl]
    ld l, a
    jp hl
.jt:
    dw .ph0, .ph1, .ph2, .ph3, .ph4
.done:
    ld a, [nes_fit_stp]
    ld e, a
    ld a, [nes_fit_stp + 1]
    ld d, a
    ld a, [nes_fit_tiles + 6]
    and a                 ; carry clear
    ret
.nta:
    ; A = NES tile column t (mod 64) -> HL = its top-row nametable byte
    and $3F
    ld c, a
    bit 5, a
    ld a, [nes_fit_pg]
    jr z, .n0
    ld a, [nes_fit_pg + 1]
.n0:
    ld h, a
    ld a, [nes_fit_rowh]
    add h
    ld h, a
    ld a, c
    and $1F
    ld l, a
    ld a, [nes_fit_rowl]
    or l
    ld l, a
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
.ph0:
    ld a, [nes_fit_tiles + 0]
    call .ptr
    ld d, h
    ld e, l
    ld a, [nes_fit_tiles + 1]
    call .ptr
    ld b, h
    ld c, l
    ld a, [nes_fit_stp]
    ld l, a
    ld a, [nes_fit_stp + 1]
    ld h, a
    ld a, [bc]
    swap a
    rrca
    and $07
    ld [hl], a
    ld a, [de]
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    swap a
    rrca
    and $07
    ld [hl], a
    ld a, [de]
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    swap a
    rrca
    and $07
    ld [hl], a
    ld a, [de]
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    swap a
    rrca
    and $07
    ld [hl], a
    ld a, [de]
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    swap a
    rrca
    and $07
    ld [hl], a
    ld a, [de]
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    swap a
    rrca
    and $07
    ld [hl], a
    ld a, [de]
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    swap a
    rrca
    and $07
    ld [hl], a
    ld a, [de]
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    swap a
    rrca
    and $07
    ld [hl], a
    ld a, [de]
    or [hl]
    ld [hli], a
    ld a, l
    ld [nes_fit_stp], a
    ld a, h
    ld [nes_fit_stp + 1], a
    ld a, [nes_fit_tiles + 3]
    call .ptr
    ld d, h
    ld e, l
    ld a, [nes_fit_tiles + 4]
    call .ptr
    ld b, h
    ld c, l
    ld a, [nes_fit_stp]
    ld l, a
    ld a, [nes_fit_stp + 1]
    ld h, a
    ld a, [bc]
    swap a
    rrca
    and $07
    ld [hl], a
    ld a, [de]
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    swap a
    rrca
    and $07
    ld [hl], a
    ld a, [de]
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    swap a
    rrca
    and $07
    ld [hl], a
    ld a, [de]
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    swap a
    rrca
    and $07
    ld [hl], a
    ld a, [de]
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    swap a
    rrca
    and $07
    ld [hl], a
    ld a, [de]
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    swap a
    rrca
    and $07
    ld [hl], a
    ld a, [de]
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    swap a
    rrca
    and $07
    ld [hl], a
    ld a, [de]
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    swap a
    rrca
    and $07
    ld [hl], a
    ld a, [de]
    or [hl]
    ld [hli], a
    ld a, l
    ld [nes_fit_stp], a
    ld a, h
    ld [nes_fit_stp + 1], a
    jp .done
.ph1:
    ld a, [nes_fit_tiles + 0]
    call .ptr
    ld d, h
    ld e, l
    ld a, [nes_fit_tiles + 1]
    call .ptr
    ld b, h
    ld c, l
    ld a, [nes_fit_stp]
    ld l, a
    ld a, [nes_fit_stp + 1]
    ld h, a
    ld a, [bc]
    swap a
    and $0F
    ld [hl], a
    ld a, [de]
    add a
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    swap a
    and $0F
    ld [hl], a
    ld a, [de]
    add a
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    swap a
    and $0F
    ld [hl], a
    ld a, [de]
    add a
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    swap a
    and $0F
    ld [hl], a
    ld a, [de]
    add a
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    swap a
    and $0F
    ld [hl], a
    ld a, [de]
    add a
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    swap a
    and $0F
    ld [hl], a
    ld a, [de]
    add a
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    swap a
    and $0F
    ld [hl], a
    ld a, [de]
    add a
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    swap a
    and $0F
    ld [hl], a
    ld a, [de]
    add a
    or [hl]
    ld [hli], a
    ld a, l
    ld [nes_fit_stp], a
    ld a, h
    ld [nes_fit_stp + 1], a
    ld a, [nes_fit_tiles + 3]
    call .ptr
    ld d, h
    ld e, l
    ld a, [nes_fit_tiles + 4]
    call .ptr
    ld b, h
    ld c, l
    ld a, [nes_fit_stp]
    ld l, a
    ld a, [nes_fit_stp + 1]
    ld h, a
    ld a, [bc]
    swap a
    and $0F
    ld [hl], a
    ld a, [de]
    add a
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    swap a
    and $0F
    ld [hl], a
    ld a, [de]
    add a
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    swap a
    and $0F
    ld [hl], a
    ld a, [de]
    add a
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    swap a
    and $0F
    ld [hl], a
    ld a, [de]
    add a
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    swap a
    and $0F
    ld [hl], a
    ld a, [de]
    add a
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    swap a
    and $0F
    ld [hl], a
    ld a, [de]
    add a
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    swap a
    and $0F
    ld [hl], a
    ld a, [de]
    add a
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    swap a
    and $0F
    ld [hl], a
    ld a, [de]
    add a
    or [hl]
    ld [hli], a
    ld a, l
    ld [nes_fit_stp], a
    ld a, h
    ld [nes_fit_stp + 1], a
    jp .done
.ph2:
    ld a, [nes_fit_tiles + 0]
    call .ptr
    ld d, h
    ld e, l
    ld a, [nes_fit_tiles + 1]
    call .ptr
    ld b, h
    ld c, l
    ld a, [nes_fit_stp]
    ld l, a
    ld a, [nes_fit_stp + 1]
    ld h, a
    ld a, [bc]
    swap a
    rlca
    ld [hl], a
    ld a, [de]
    add a
    add a
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    swap a
    rlca
    ld [hl], a
    ld a, [de]
    add a
    add a
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    swap a
    rlca
    ld [hl], a
    ld a, [de]
    add a
    add a
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    swap a
    rlca
    ld [hl], a
    ld a, [de]
    add a
    add a
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    swap a
    rlca
    ld [hl], a
    ld a, [de]
    add a
    add a
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    swap a
    rlca
    ld [hl], a
    ld a, [de]
    add a
    add a
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    swap a
    rlca
    ld [hl], a
    ld a, [de]
    add a
    add a
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    swap a
    rlca
    ld [hl], a
    ld a, [de]
    add a
    add a
    or [hl]
    ld [hli], a
    ld a, l
    ld [nes_fit_stp], a
    ld a, h
    ld [nes_fit_stp + 1], a
    ld a, [nes_fit_tiles + 3]
    call .ptr
    ld d, h
    ld e, l
    ld a, [nes_fit_tiles + 4]
    call .ptr
    ld b, h
    ld c, l
    ld a, [nes_fit_stp]
    ld l, a
    ld a, [nes_fit_stp + 1]
    ld h, a
    ld a, [bc]
    swap a
    rlca
    ld [hl], a
    ld a, [de]
    add a
    add a
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    swap a
    rlca
    ld [hl], a
    ld a, [de]
    add a
    add a
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    swap a
    rlca
    ld [hl], a
    ld a, [de]
    add a
    add a
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    swap a
    rlca
    ld [hl], a
    ld a, [de]
    add a
    add a
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    swap a
    rlca
    ld [hl], a
    ld a, [de]
    add a
    add a
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    swap a
    rlca
    ld [hl], a
    ld a, [de]
    add a
    add a
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    swap a
    rlca
    ld [hl], a
    ld a, [de]
    add a
    add a
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    swap a
    rlca
    ld [hl], a
    ld a, [de]
    add a
    add a
    or [hl]
    ld [hli], a
    ld a, l
    ld [nes_fit_stp], a
    ld a, h
    ld [nes_fit_stp + 1], a
    jp .done
.ph3:
    ld a, [nes_fit_tiles + 0]
    call .ptr
    ld d, h
    ld e, l
    ld a, [nes_fit_tiles + 1]
    call .ptr
    ld b, h
    ld c, l
    ld a, [nes_fit_stp]
    ld l, a
    ld a, [nes_fit_stp + 1]
    ld h, a
    ld a, [bc]
    srl a
    srl a
    ld [hl], a
    ld a, [de]
    add a
    add a
    add a
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    srl a
    srl a
    ld [hl], a
    ld a, [de]
    add a
    add a
    add a
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    srl a
    srl a
    ld [hl], a
    ld a, [de]
    add a
    add a
    add a
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    srl a
    srl a
    ld [hl], a
    ld a, [de]
    add a
    add a
    add a
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    srl a
    srl a
    ld [hl], a
    ld a, [de]
    add a
    add a
    add a
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    srl a
    srl a
    ld [hl], a
    ld a, [de]
    add a
    add a
    add a
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    srl a
    srl a
    ld [hl], a
    ld a, [de]
    add a
    add a
    add a
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    srl a
    srl a
    ld [hl], a
    ld a, [de]
    add a
    add a
    add a
    or [hl]
    ld [hli], a
    ld a, l
    sub 8
    ld l, a
    push hl
    ld a, [nes_fit_tiles + 2]
    call .ptr
    ld b, h
    ld c, l
    pop hl
    ld a, [bc]
    rlca
    and $01
    or [hl]
    ld [hli], a
    inc c
    ld a, [bc]
    rlca
    and $01
    or [hl]
    ld [hli], a
    inc c
    ld a, [bc]
    rlca
    and $01
    or [hl]
    ld [hli], a
    inc c
    ld a, [bc]
    rlca
    and $01
    or [hl]
    ld [hli], a
    inc c
    ld a, [bc]
    rlca
    and $01
    or [hl]
    ld [hli], a
    inc c
    ld a, [bc]
    rlca
    and $01
    or [hl]
    ld [hli], a
    inc c
    ld a, [bc]
    rlca
    and $01
    or [hl]
    ld [hli], a
    inc c
    ld a, [bc]
    rlca
    and $01
    or [hl]
    ld [hli], a
    ld a, l
    ld [nes_fit_stp], a
    ld a, h
    ld [nes_fit_stp + 1], a
    ld a, [nes_fit_tiles + 3]
    call .ptr
    ld d, h
    ld e, l
    ld a, [nes_fit_tiles + 4]
    call .ptr
    ld b, h
    ld c, l
    ld a, [nes_fit_stp]
    ld l, a
    ld a, [nes_fit_stp + 1]
    ld h, a
    ld a, [bc]
    srl a
    srl a
    ld [hl], a
    ld a, [de]
    add a
    add a
    add a
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    srl a
    srl a
    ld [hl], a
    ld a, [de]
    add a
    add a
    add a
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    srl a
    srl a
    ld [hl], a
    ld a, [de]
    add a
    add a
    add a
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    srl a
    srl a
    ld [hl], a
    ld a, [de]
    add a
    add a
    add a
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    srl a
    srl a
    ld [hl], a
    ld a, [de]
    add a
    add a
    add a
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    srl a
    srl a
    ld [hl], a
    ld a, [de]
    add a
    add a
    add a
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    srl a
    srl a
    ld [hl], a
    ld a, [de]
    add a
    add a
    add a
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    srl a
    srl a
    ld [hl], a
    ld a, [de]
    add a
    add a
    add a
    or [hl]
    ld [hli], a
    ld a, l
    sub 8
    ld l, a
    push hl
    ld a, [nes_fit_tiles + 5]
    call .ptr
    ld b, h
    ld c, l
    pop hl
    ld a, [bc]
    rlca
    and $01
    or [hl]
    ld [hli], a
    inc c
    ld a, [bc]
    rlca
    and $01
    or [hl]
    ld [hli], a
    inc c
    ld a, [bc]
    rlca
    and $01
    or [hl]
    ld [hli], a
    inc c
    ld a, [bc]
    rlca
    and $01
    or [hl]
    ld [hli], a
    inc c
    ld a, [bc]
    rlca
    and $01
    or [hl]
    ld [hli], a
    inc c
    ld a, [bc]
    rlca
    and $01
    or [hl]
    ld [hli], a
    inc c
    ld a, [bc]
    rlca
    and $01
    or [hl]
    ld [hli], a
    inc c
    ld a, [bc]
    rlca
    and $01
    or [hl]
    ld [hli], a
    ld a, l
    ld [nes_fit_stp], a
    ld a, h
    ld [nes_fit_stp + 1], a
    jp .done
.ph4:
    ld a, [nes_fit_tiles + 0]
    call .ptr
    ld d, h
    ld e, l
    ld a, [nes_fit_tiles + 1]
    call .ptr
    ld b, h
    ld c, l
    ld a, [nes_fit_stp]
    ld l, a
    ld a, [nes_fit_stp + 1]
    ld h, a
    ld a, [bc]
    srl a
    ld [hl], a
    ld a, [de]
    swap a
    and $F0
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    srl a
    ld [hl], a
    ld a, [de]
    swap a
    and $F0
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    srl a
    ld [hl], a
    ld a, [de]
    swap a
    and $F0
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    srl a
    ld [hl], a
    ld a, [de]
    swap a
    and $F0
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    srl a
    ld [hl], a
    ld a, [de]
    swap a
    and $F0
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    srl a
    ld [hl], a
    ld a, [de]
    swap a
    and $F0
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    srl a
    ld [hl], a
    ld a, [de]
    swap a
    and $F0
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    srl a
    ld [hl], a
    ld a, [de]
    swap a
    and $F0
    or [hl]
    ld [hli], a
    ld a, l
    sub 8
    ld l, a
    push hl
    ld a, [nes_fit_tiles + 2]
    call .ptr
    ld b, h
    ld c, l
    pop hl
    ld a, [bc]
    rlca
    rlca
    and $03
    or [hl]
    ld [hli], a
    inc c
    ld a, [bc]
    rlca
    rlca
    and $03
    or [hl]
    ld [hli], a
    inc c
    ld a, [bc]
    rlca
    rlca
    and $03
    or [hl]
    ld [hli], a
    inc c
    ld a, [bc]
    rlca
    rlca
    and $03
    or [hl]
    ld [hli], a
    inc c
    ld a, [bc]
    rlca
    rlca
    and $03
    or [hl]
    ld [hli], a
    inc c
    ld a, [bc]
    rlca
    rlca
    and $03
    or [hl]
    ld [hli], a
    inc c
    ld a, [bc]
    rlca
    rlca
    and $03
    or [hl]
    ld [hli], a
    inc c
    ld a, [bc]
    rlca
    rlca
    and $03
    or [hl]
    ld [hli], a
    ld a, l
    ld [nes_fit_stp], a
    ld a, h
    ld [nes_fit_stp + 1], a
    ld a, [nes_fit_tiles + 3]
    call .ptr
    ld d, h
    ld e, l
    ld a, [nes_fit_tiles + 4]
    call .ptr
    ld b, h
    ld c, l
    ld a, [nes_fit_stp]
    ld l, a
    ld a, [nes_fit_stp + 1]
    ld h, a
    ld a, [bc]
    srl a
    ld [hl], a
    ld a, [de]
    swap a
    and $F0
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    srl a
    ld [hl], a
    ld a, [de]
    swap a
    and $F0
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    srl a
    ld [hl], a
    ld a, [de]
    swap a
    and $F0
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    srl a
    ld [hl], a
    ld a, [de]
    swap a
    and $F0
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    srl a
    ld [hl], a
    ld a, [de]
    swap a
    and $F0
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    srl a
    ld [hl], a
    ld a, [de]
    swap a
    and $F0
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    srl a
    ld [hl], a
    ld a, [de]
    swap a
    and $F0
    or [hl]
    ld [hli], a
    inc c
    inc e
    ld a, [bc]
    srl a
    ld [hl], a
    ld a, [de]
    swap a
    and $F0
    or [hl]
    ld [hli], a
    ld a, l
    sub 8
    ld l, a
    push hl
    ld a, [nes_fit_tiles + 5]
    call .ptr
    ld b, h
    ld c, l
    pop hl
    ld a, [bc]
    rlca
    rlca
    and $03
    or [hl]
    ld [hli], a
    inc c
    ld a, [bc]
    rlca
    rlca
    and $03
    or [hl]
    ld [hli], a
    inc c
    ld a, [bc]
    rlca
    rlca
    and $03
    or [hl]
    ld [hli], a
    inc c
    ld a, [bc]
    rlca
    rlca
    and $03
    or [hl]
    ld [hli], a
    inc c
    ld a, [bc]
    rlca
    rlca
    and $03
    or [hl]
    ld [hli], a
    inc c
    ld a, [bc]
    rlca
    rlca
    and $03
    or [hl]
    ld [hli], a
    inc c
    ld a, [bc]
    rlca
    rlca
    and $03
    or [hl]
    ld [hli], a
    inc c
    ld a, [bc]
    rlca
    rlca
    and $03
    or [hl]
    ld [hli], a
    ld a, l
    ld [nes_fit_stp], a
    ld a, h
    ld [nes_fit_stp + 1], a
    jp .done

; Dirty nametable cells -> dirty slots of every view position showing them.
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
    ld hl, nes_fit_sig + 4
    ld c, 0
    call .hud_half
    ld hl, nes_fit_sig + 5
    ld c, 16
.hud_half:
    ld a, [hl]
    cp d
    ret nz
    call .w1
    push af
    call .hud_w
    pop af
    inc a
    cp 40
    jr c, .hud_w
    xor a
.hud_w:
    ; HUD column = w - hud w0 (mod 40), shown if < 21
    ld hl, nes_fit_sig + 3
    sub [hl]
    jr nc, .hp
    add 40
.hp:
    cp 21
    ret nc
    jp nes_fit_mark_rc
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
    call .w1
    push af
    call .ring_w
    pop af
    inc a
    cp 40
    jr c, .ring_w
    xor a
.ring_w:
    add LOW(nes_fit_wmap)
    ld l, a
    ld h, HIGH(nes_fit_wmap)
    ld a, [hl]
    cp $FF
    ret z
    jp nes_fit_mark_rc
.w1:
    ; A = first world column touched by cell column (E & 15) | C
    ld a, e
    and $0F
    or c
    add LOW(nes_fitw_c2w)
    ld l, a
    adc HIGH(nes_fitw_c2w)
    sub l
    ld h, a
    ld a, [hl]
    ret

; ISR tail (any time, VRAM not needed except CHR-RAM tile rows): refresh the
; cache, then compose up to NES2GBC_FIT_CAP dirty slots into the stage.
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
    jp z, .out
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
    ; byte c, bit b -> row = c>>2, map column = (c&3)*8 + b
    ld a, c
    and $03
    add a
    add a
    add a
    or b
    ld l, a
    ld a, c
    srl a
    srl a
    ld b, a
    ld c, l
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
    cp 60
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

; VBlank: GDMA every staged slot into VRAM ($8800 + i*16, bank from its
; attribute) and write its map attribute (+ the static id).
nes_fit_flush:
    ldh a, [rSVBK]
    push af
    ld a, $07
    ldh [rSVBK], a
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
    ld hl, nes_fit_stage_attr
    call nes_fit_compose_budget.idx
    ld a, [hl]
    rrca
    rrca
    rrca
    and $01
    ldh [rVBK], a
    ld a, d
    ldh [FIT_rHDMA1], a
    ld a, e
    ldh [FIT_rHDMA2], a
    ld hl, nes_fit_stage_slot
    call nes_fit_compose_budget.idx
    ld a, [hl]
    swap a
    ld l, a
    and $0F
    add $08             ; $8800 + i*16 -> HDMA3 takes bits 12-4
    ldh [FIT_rHDMA3], a
    ld a, l
    and $F0
    ldh [FIT_rHDMA4], a
    xor a
    ldh [FIT_rHDMA5], a ; one 16-byte block, general DMA
    push de
    ld hl, nes_fit_stage_mlo
    call nes_fit_compose_budget.idx
    ld e, [hl]
    ld hl, nes_fit_stage_mhi
    call nes_fit_compose_budget.idx
    ld d, [hl]
    ld hl, nes_fit_stage_attr
    call nes_fit_compose_budget.idx
    ld a, $01
    ldh [rVBK], a
    ld a, [hl]
    ld [de], a
    pop de
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

; B = PPUCTRL (bit 0 = nametable X), A = scroll X -> HL = scaled X
; (0-319) = (256*nt + x) * 5 / 8. Keeps BC.
nes_fit_scale_x:
    ld l, a
    ld a, b
    and $01
    ld h, a
    push de
    ld d, h
    ld e, l
    add hl, hl
    add hl, hl
    add hl, de
    pop de
    srl h
    rr l
    srl h
    rr l
    srl h
    rr l
    ret

; HL = scaled X -> A = world column X/8 (0-39)
nes_fit_x_col:
    ld a, l
    rrca
    rrca
    rrca
    and $1F
    bit 0, h
    ret z
    or $20
    ret

; VBlank after a commit: playfield scroll -> ring position / SCX, HUD split ->
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
    push de
    ld a, d
    call nes_fit_scale_x
    call nes_fit_x_col
    ld [nes_fit_w0], a
    ; delta = X - last X, wrapped to [-160, 160) (mod 320); R += delta
    ld a, [nes_fit_lastx]
    ld e, a
    ld a, [nes_fit_lastx + 1]
    ld d, a
    ld a, l
    ld [nes_fit_lastx], a
    sub e
    ld e, a
    ld a, h
    ld [nes_fit_lastx + 1], a
    sbc d
    ld d, a
    bit 7, d
    jr nz, .neg
    and a
    jr nz, .sub
    ld a, e
    cp 160
    jr c, .dok
.sub:
    ld a, e
    sub $40               ; - 320 (low byte)
    ld e, a
    jr .dok
.neg:
    inc a
    jr nz, .add
    ld a, e
    cp $60
    jr nc, .dok
.add:
    ld a, e
    add $40               ; + 320 (low byte)
    ld e, a
.dok:
    ld a, [nes_fit_ring_scx]
    add e
    ld [nes_fit_ring_scx], a
    pop de
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
    call nes_fit_scale_x
    call nes_fit_x_col
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
    ; columns only move when the ring position or w0 does
    ld a, [nes_fit_ring_scx]
    rrca
    rrca
    rrca
    and $1F
    ld hl, nes_fit_last_mc
    cp [hl]
    ld [hl], a
    jr nz, nes_fit_assign
    ld a, [nes_fit_w0]
    ld hl, nes_fit_last_w0
    cp [hl]
    ret z
    ; fall through

; Visible map column (R/8 + i) holds world column (w0 + i) mod 40, i < 21;
; other columns are released. Changed columns are marked for rows K..14.
nes_fit_assign:
    ld a, [nes_fit_w0]
    ld [nes_fit_last_w0], a
    ld e, a
    ld hl, nes_fit_newown
    ld b, 32
    ld a, $FF
.f:
    ld [hli], a
    dec b
    jr nz, .f
    ld a, [nes_fit_last_mc]
    ld c, a
    ld b, 21
    ld h, HIGH(nes_fit_newown)
.v:
    ld a, c
    and $1F
    add LOW(nes_fit_newown)
    ld l, a
    ld [hl], e
    inc c
    inc e
    ld a, e
    cp 40
    jr c, .vk
    ld e, 0
.vk:
    dec b
    jr nz, .v
    ; pass 1: forget the old owners of changed columns
    ld c, 0
.p1:
    ld a, c
    add LOW(nes_fit_owner)
    ld l, a
    ld a, [hl]
    ld b, a
    ld a, l
    add 32
    ld l, a
    ld a, [hl]
    cp b
    jr z, .p1n
    ld a, b
    cp $FF
    jr z, .p1n
    add LOW(nes_fit_wmap)
    ld l, a
    ld a, [hl]
    cp c
    jr nz, .p1n
    ld [hl], $FF
.p1n:
    inc c
    ld a, c
    cp 32
    jr nz, .p1
    ; pass 2: take the new owners, mark their rows
    ld c, 0
.p2:
    ld h, HIGH(nes_fit_owner)
    ld a, c
    add LOW(nes_fit_newown)
    ld l, a
    ld a, [hl]
    ld b, a
    ld a, c
    add LOW(nes_fit_owner)
    ld l, a
    ld a, [hl]
    cp b
    jr z, .p2n
    ld [hl], b
    ld a, b
    cp $FF
    jr z, .p2n
    add LOW(nes_fit_wmap)
    ld l, a
    ld [hl], c
    ld a, [nes_fit_sig + 2]
    ld b, a
.mk:
    ld a, b
    cp 15
    jr nc, .p2n
    push bc
    ld a, c
    call nes_fit_mark_rc
    pop bc
    inc b
    jr .mk
.p2n:
    inc c
    ld a, c
    cp 32
    jr nz, .p2
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
    ld b, 0
.hr:
    ld c, 0
.hc:
    push bc
    ld a, c
    call nes_fit_mark_rc
    pop bc
    inc c
    ld a, c
    cp 21
    jr nz, .hc
    inc b
    ld a, [nes_fit_sig + 2]
    cp b
    jr nz, .hr
    ret

; HUD fields changed but the HUD may show the same bytes (SMB keeps its
; status bar in both nametables and flips the HUD page as the world scrolls).
; Both views page-aligned (w0 = 0 / 20): compare the K visible cell rows and
; their attributes. Z = same (nothing to redraw).
nes_fit_hud_same:
    ld a, [nes_fit_osig + 3]
    and a
    jr z, .oa
    cp 20
    ret nz
.oa:
    ld b, a
    ld a, [nes_fit_sig + 3]
    and a
    jr z, .na
    cp 20
    ret nz
.na:
    ld hl, nes_fit_osig + 4
    ld a, b
    and a
    jr z, .o
    inc hl
.o:
    ld d, [hl]
    ld hl, nes_fit_sig + 4
    ld a, [nes_fit_sig + 3]
    and a
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

; VBlank PPUCTRL commit: sprite size, and the sprite pattern table held at
; $8000 (CHR-RAM: re-shrink its tiles; CHR-ROM: GDMA it over 4 VBlanks).
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
    ld a, [nes_ppuctrl]
    and $08
    rrca
    rrca
    rrca
    ld hl, nes_fit_spt
    cp [hl]
    ret z
    ld [hl], a
    ld b, a
    ld a, [nes_chr_is_ram]
    and a
    jr z, .rom
    ld a, b
    swap a
    add a
    add LOW(nes_fit_chr_dirty)
    ld l, a
    adc HIGH(nes_fit_chr_dirty)
    sub l
    ld h, a
    ld a, $FF
    ld b, 32
.d:
    ld [hli], a
    dec b
    jr nz, .d
    ld [nes_fit_chr_any], a
    ret
.rom:
    ld a, 4
    ld [nes_fit_spr_pend], a
    ret

; Force the FIT display: no window, HUD map above LYC, scrolled ring below.
nes_fit_display:
    ld a, 167
    ldh [rWX], a
    xor a
    ldh [rWY], a
    ld a, $F4
    ldh [rSCY], a
    ldh a, [rLCDC]
    and $87
    ld b, a
    ld a, [nes_fit_sig + 2]
    and a
    jr z, .ring
    ld a, [nes_fit_sig + 2]
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
    ld a, [nes_fit_spr_pend]
    and a
    call nz, nes_fit_spr_gdma
    ld a, $FF
    ld [$2000], a
    call nes_fit_frame_x
    jp nes_restore_code_bank
