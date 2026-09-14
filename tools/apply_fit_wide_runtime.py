#!/usr/bin/env python3
from pathlib import Path


def replace_once(s: str, old: str, new: str, label: str) -> str:
    if old not in s:
        raise SystemExit(f"missing pattern: {label}")
    return s.replace(old, new, 1)


def patch_assets(root: Path) -> None:
    p = root / "src/assets.rs"
    s = p.read_text()
    s = s.replace(
        "//! `convert_chr_to_gbc_fit_half` also nearest-neighbor shrinks each 8x8 tile\n//! to a 4x4 (2x2 pixel boxes), then parks that content in the top-left of an\n//! 8x8 GBC tile with color-0 padding. Pair with runtime OAM coordinate\n//! halving so multi-sprite objects still abut.\n",
        "//! `convert_chr_to_gbc_fit_wide` scales each 8x8 NES tile to a 5x4 crumb.\n//! The runtime places those crumbs at X*5/8, Y/2, producing a 160x120 4:3\n//! presentation from the NES 256x240 raster while preserving 12px letterbox\n//! bars vertically. Content is parked top-left in an 8x8 GBC sprite tile.\n",
    )
    start = s.index("/// 2x2 box sample")
    end = s.index("pub fn convert_chr_to_gbc(chr")
    helper = r'''/// Box sample used by fit scaling: prefer a non-zero pixel so one-pixel
/// outlines survive reduction instead of disappearing into color 0.
fn sample_box(px: &[[u8; 8]; 8], r0: usize, r1: usize, c0: usize, c1: usize) -> u8 {
    for r in r0..r1 {
        for c in c0..c1 {
            let p = px[r][c];
            if p != 0 {
                return p;
            }
        }
    }
    0
}

fn shrink_tile_wide(tile: &[u8]) -> [[u8; 8]; 8] {
    let src = nes_tile_pixels(tile);
    let mut dst = [[0u8; 8]; 8];
    // floor(x*5/8) bins: 0-1, 2-3, 4, 5-6, 7.
    const X0: [usize; 5] = [0, 2, 4, 5, 7];
    const X1: [usize; 5] = [2, 4, 5, 7, 8];
    for r in 0..4 {
        for c in 0..5 {
            dst[r][c] = sample_box(&src, r * 2, r * 2 + 2, X0[c], X1[c]);
        }
    }
    dst
}

'''
    s = s[:start] + helper + s[end:]
    old = r'''/// Build-time half-resolution CHR for fit-screen mode.
pub fn convert_chr_to_gbc_fit_half(chr: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(chr.len());
    for tile in chr.chunks(16) {
        if tile.len() < 16 {
            out.extend_from_slice(tile);
            continue;
        }
        let shrunk = shrink_tile_half(tile);
        encode_gbc_tile(&shrunk, &mut out);
    }
    out
}
'''
    new = r'''/// Build-time 5x4 CHR crumbs for the 160x120 fit-screen mode.
pub fn convert_chr_to_gbc_fit_wide(chr: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(chr.len());
    for tile in chr.chunks(16) {
        if tile.len() < 16 {
            out.extend_from_slice(tile);
            continue;
        }
        let shrunk = shrink_tile_wide(tile);
        encode_gbc_tile(&shrunk, &mut out);
    }
    out
}
'''
    s = replace_once(s, old, new, "fit converter")
    s = s.replace("fn fit_half_keeps_tile_size_and_collapses_to_top_left()", "fn fit_wide_keeps_tile_size_and_collapses_to_top_left()")
    s = s.replace("convert_chr_to_gbc_fit_half(&tile)", "convert_chr_to_gbc_fit_wide(&tile)")
    s = s.replace("// Rows 0..3: left nibble solid (cols 0..3), right clear.\n        for row in 0..4 {\n            assert_eq!(out[row * 2], 0xF0, \"lo plane row {row}\");\n            assert_eq!(out[row * 2 + 1], 0xF0, \"hi plane row {row}\");\n", "// Rows 0..3: five left pixels solid, three padded transparent.\n        for row in 0..4 {\n            assert_eq!(out[row * 2], 0xF8, \"lo plane row {row}\");\n            assert_eq!(out[row * 2 + 1], 0xF8, \"hi plane row {row}\");\n")
    s = s.replace("fn fit_half_preserves_nonzero_in_2x2()", "fn fit_wide_preserves_nonzero_in_box()")
    p.write_text(s)


def patch_main_rs(root: Path) -> None:
    p = root / "src/main.rs"
    s = p.read_text()
    s = s.replace("; Build-time half-scale fit-screen mode", "; Build-time 160x120 4:3 fit-screen mode")
    s = s.replace('println!("Fit-screen: half-scale CHR (4x4 content in 8x8 GBC tiles)");\n            assets::convert_chr_to_gbc_fit_half(cart.chr_rom)', 'println!("Fit-screen: 160x120 4:3 CHR (5x4 content in 8x8 GBC tiles)");\n            assets::convert_chr_to_gbc_fit_wide(cart.chr_rom)')
    p.write_text(s)


def patch_main_asm(root: Path) -> None:
    p = root / "runtime/main.asm"
    s = p.read_text()
    old = r'''    ; Half-scale + letterbox top/HUD scroll. Same physical identity map.
    ldh a, [nes_split_armed_top_x]
    srl a
    sub 16
    ldh [rSCX], a
    ldh a, [nes_split_armed_top_y]
    srl a
    sub 12
    ldh [rSCY], a
'''
    new = r'''    ; 160x120 fit: X uses 5/8 NES scale; Y stays half-scale with 12px bars.
    ldh a, [nes_split_armed_top_x]
    ld c, a
    ld b, $00
    call nes_video_fit_scale_x_bc
    ld a, l
    ldh [rSCX], a
    ldh a, [nes_split_armed_top_y]
    srl a
    sub 12
    ldh [rSCY], a
'''
    s = replace_once(s, old, new, "fit split top scale")
    p.write_text(s)


def patch_video_prefix(root: Path, s: str) -> str:
    # Initial LCD mode: signed BG tile addressing in fit mode ($8800-$97FF),
    # ordinary unsigned addressing otherwise.
    old = r'''    ; LCD on, BG on, unsigned tile IDs, map $9800.
    ld a, $91
    ldh [rLCDC], a
    ret
'''
    new = r'''    ; LCD on, BG on, map $9800. Fit uses signed BG tile IDs so the
    ; $8800-$97FF BG region can coexist with sprites in $8000-$87FF.
    ld a, [nes_fit_screen]
    and a
    ld a, $91
    jr z, .init_lcdc_store
    ld a, $81
.init_lcdc_store:
    ldh [rLCDC], a
    ret
'''
    s = replace_once(s, old, new, "initial LCDC signed fit")

    old = r'''    ; Half-scale + vertical letterbox: (144-120)/2 = 12.
    ldh a, [nes_view_coord_tmp]
    srl a
    add 12
    cp $90
    jp nc, .skip_three_source_bytes
    add $10
    ldh [nes_oam_proj_y_tmp], a
'''
    new = r'''    ; 160x120 fit keeps the proven half-height vertical mapping.
    ldh a, [nes_view_coord_tmp]
    srl a
    add 12
    cp $90
    jp nc, .skip_three_source_bytes
    add $10
    ldh [nes_oam_proj_y_tmp], a
'''
    s = replace_once(s, old, new, "fit sprite y comment")

    old = r'''    ; Half-scale + horizontal letterbox: (160-128)/2 = 16.
    ldh a, [nes_view_coord_tmp]
    srl a
    add 16
    cp $A0
    jp nc, .next_source
    add $08
    ldh [nes_oam_proj_x_tmp], a

.x_ready:

    ; Visible sprite: pack it into the next CGB OAM slot.
'''
    new = r'''    ; 160px-wide fit: X = floor(NES_X * 5/8), no side letterbox.
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
'''
    s = replace_once(s, old, new, "fit sprite x scale")

    old = r'''    ; Tile number and pattern-table bank.
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
'''
    new = r'''    ; Tile number and pattern-table bank. Fit stores the active 256-tile
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
'''
    s = replace_once(s, old, new, "fit sprite split banks")

    old = r'''.fit_store:
.store:
    ld a, b
.write:
    ldh [rLCDC], a
    ret
'''
    new = r'''.fit_store:
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
'''
    s = replace_once(s, old, new, "fit LCDC signed addressing")
    return s


FIT_SECTION = r'''; ---------------------------------------------------------------------------
; Fit-screen 160x120 4:3 scaler
; ---------------------------------------------------------------------------
; NES 256x240 -> GBC 160x120: X = 5/8, Y = 1/2. This matches the NES's
; intended 4:3 display aspect while using the full GBC width and retaining only
; 12px letterbox bars above/below. Each NES 8x8 CHR tile is preconverted to a
; 5x4 crumb. Two NES tile rows therefore align exactly with one 8px GBC row.
;
; BG uses signed tile addressing ($8800-$97FF). 20x15 = 300 identity slots are
; split across VRAM bank0 (slots 0-255) and bank1 (slots 256-299). The lower
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
; signed tile $AC (local slot 44, immediately after the 300 live BG slots).
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
    ld d, $AC
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
    cp b
    jr z, .page_done
    ld a, b
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

; 20-column circular visible window over the 40 GBC tile columns that represent
; the 512px two-nametable horizontal NES world at 5/8 scale.
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
    jr z, .resident_origin_ok
    xor a
    ld [nes_fit_origin_mx], a
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
    jr z, .scx_from_origin
    ld b, a
    ld a, c
    ld [nes_fit_origin_mx], a

    ld a, [nes_fit_dirty]
    and a
    jr nz, .origin_while_dirty

    ld a, c
    sub b
    cp 1
    jr z, .delta_plus1
    cp $FF
    jr z, .delta_minus1
    jr .full_dirty

.delta_plus1:
    ld a, 2
    ld [nes_fit_dirty], a
    xor a
    ld [nes_fit_recompose_my], a
    ld a, 19
    ld [nes_fit_mt_mx], a
    jr .scx_from_origin
.delta_minus1:
    ld a, 3
    ld [nes_fit_dirty], a
    xor a
    ld [nes_fit_recompose_my], a
    ld [nes_fit_mt_mx], a
    jr .scx_from_origin
.origin_while_dirty:
    ld a, [nes_fit_dirty]
    cp 1
    jr z, .scx_from_origin
.full_dirty:
    ld a, $01
    ld [nes_fit_dirty], a
    xor a
    ld [nes_fit_recompose_my], a
    ld [nes_fit_mt_mx], a

.scx_from_origin:
    ld a, [nes_fit_origin_mx]
    and $1F
    add a
    add a
    add a
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

    ; Full rebuild: at most one 20-tile row per LCD-on call. Entering columns
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
    ld a, 19
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
    cp 20
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
    cp 20
    jr nc, .second_done
    ld [nes_fit_mt_quad + 3], a
.second_done:

    ; First destination if visible in [origin, origin+20).
    ld a, [nes_fit_origin_mx]
    ld c, a
    ld a, d
    sub c
    jr nc, .first_delta_ready
    add 40
.first_delta_ready:
    cp 20
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

    ; slot_col = world_col % 20.
    call nes_video_fit_world_col
    cp 20
    jr c, .slot_col_ready
    sub 20
.slot_col_ready:
    ld [nes_fit_mt_page], a

    ; slot = my*20 + slot_col. D=0/1 selects BG VRAM bank.
    ld a, [nes_fit_mt_my]
    ld b, a
    swap a
    and $F0
    ld e, a
    ld a, b
    add a
    add a
    ld d, $00
    add e
    jr nc, .slot_no_carry0
    inc d
.slot_no_carry0:
    ld b, a
    ld a, [nes_fit_mt_page]
    add b
    jr nc, .slot_no_carry1
    inc d
.slot_no_carry1:
    add $80
    ld [nes_fit_mt_tmp_l], a   ; signed BG tile number

    ld a, c
    ld b, a
    ld a, d
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
    call nes_video_fit_world_col
    and $1F
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
    ld a, [nes_fit_mt_page]
    ld c, a
    ld a, [nes_fit_mt_quad]
    call nes_video_fit_get_scaled_row_a_c
    ld a, d
    ld [nes_fit_mt_compose + 10], a
    ld a, e
    ld [nes_fit_mt_compose + 11], a

    ld a, [nes_fit_mt_page]
    ld c, a
    ld a, [nes_fit_mt_quad + 1]
    call nes_video_fit_get_scaled_row_a_c
    ld a, d
    ld [nes_fit_mt_compose + 12], a
    ld a, e
    ld [nes_fit_mt_compose + 13], a

    ld a, [nes_fit_mt_page]
    ld c, a
    ld a, [nes_fit_mt_quad + 2]
    call nes_video_fit_get_scaled_row_a_c
    ld a, d
    ld [nes_fit_mt_compose + 14], a
    ld a, e
    ld [nes_fit_mt_compose + 15], a

    ld a, [nes_fit_mt_compose + 10]
    ld b, a
    ld a, [nes_fit_mt_compose + 12]
    ld c, a
    ld a, [nes_fit_mt_compose + 14]
    ld d, a
    call nes_video_fit_pack_plane
    ld [nes_fit_mt_compose + 10], a

    ld a, [nes_fit_mt_compose + 11]
    ld b, a
    ld a, [nes_fit_mt_compose + 13]
    ld c, a
    ld a, [nes_fit_mt_compose + 15]
    ld d, a
    call nes_video_fit_pack_plane
    ld [nes_fit_mt_compose + 11], a

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
    ld a, [nes_fit_mt_compose + 10]
    ld [hli], a
    ld a, [nes_fit_mt_compose + 11]
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
'''


def patch_video(root: Path) -> None:
    p = root / "runtime/video.asm"
    s = p.read_text()
    s = patch_video_prefix(root, s)
    marker = "; ---------------------------------------------------------------------------\n; Fit-screen identity metatiles: pack 2x2 half-CHR crumbs into tile 1+my*16+mx\n; ---------------------------------------------------------------------------\n"
    if marker not in s:
        raise SystemExit("missing fit section marker")
    s = s[:s.index(marker)] + FIT_SECTION
    p.write_text(s)


def main() -> None:
    root = Path(__file__).resolve().parent.parent if (Path(__file__).resolve().parent / "../src").exists() else Path.cwd()
    # Workflow copies this script to /tmp; use cwd when repository files are there.
    if not (root / "src").exists():
        root = Path.cwd()
    patch_assets(root)
    patch_main_rs(root)
    patch_main_asm(root)
    patch_video(root)
    print("applied 160x120 wide fit-screen patch")


if __name__ == "__main__":
    main()
