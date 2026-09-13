//! CHR asset conversion for the GBC host.
//!
//! `convert_chr_to_gbc` only reorders NES bitplanes into GBC row format.
//! `convert_chr_to_gbc_fit_half` also nearest-neighbor shrinks each 8x8 tile
//! to a 4x4 (2x2 pixel boxes), then parks that content in the top-left of an
//! 8x8 GBC tile with color-0 padding. Pair with runtime OAM coordinate
//! halving so multi-sprite objects still abut.

fn nes_tile_pixels(tile: &[u8]) -> [[u8; 8]; 8] {
    let mut px = [[0u8; 8]; 8];
    for row in 0..8 {
        let lo = tile[row];
        let hi = tile[row + 8];
        for col in 0..8 {
            let bit = 7 - col;
            let p0 = (lo >> bit) & 1;
            let p1 = (hi >> bit) & 1;
            px[row][col] = p0 | (p1 << 1);
        }
    }
    px
}

fn encode_gbc_tile(px: &[[u8; 8]; 8], out: &mut Vec<u8>) {
    for row in 0..8 {
        let mut lo = 0u8;
        let mut hi = 0u8;
        for col in 0..8 {
            let bit = 7 - col;
            let p = px[row][col] & 3;
            if p & 1 != 0 {
                lo |= 1 << bit;
            }
            if p & 2 != 0 {
                hi |= 1 << bit;
            }
        }
        out.push(lo);
        out.push(hi);
    }
}

/// 2x2 box sample: prefer any non-zero pixel so thin outlines survive.
fn sample_2x2(px: &[[u8; 8]; 8], r: usize, c: usize) -> u8 {
    let mut best = 0u8;
    for dr in 0..2 {
        for dc in 0..2 {
            let p = px[r + dr][c + dc];
            if p != 0 {
                return p;
            }
            best = best.max(p);
        }
    }
    best
}

fn shrink_tile_half(tile: &[u8]) -> [[u8; 8]; 8] {
    let src = nes_tile_pixels(tile);
    let mut dst = [[0u8; 8]; 8];
    for r in 0..4 {
        for c in 0..4 {
            dst[r][c] = sample_2x2(&src, r * 2, c * 2);
        }
    }
    dst
}

pub fn convert_chr_to_gbc(chr: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(chr.len());

    for tile in chr.chunks(16) {
        if tile.len() < 16 {
            out.extend_from_slice(tile);
            continue;
        }

        for row in 0..8 {
            out.push(tile[row]);
            out.push(tile[row + 8]);
        }
    }

    out
}

/// Build-time half-resolution CHR for fit-screen mode.
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn interleaves_nes_bitplanes_by_row() {
        let input: Vec<u8> = (0u8..16).collect();
        let output = convert_chr_to_gbc(&input);
        assert_eq!(output, vec![0, 8, 1, 9, 2, 10, 3, 11, 4, 12, 5, 13, 6, 14, 7, 15]);
    }

    #[test]
    fn fit_half_keeps_tile_size_and_collapses_to_top_left() {
        // Solid color-3 tile.
        let tile = vec![0xFFu8; 16];
        let out = convert_chr_to_gbc_fit_half(&tile);
        assert_eq!(out.len(), 16);
        // Rows 0..3: left nibble solid (cols 0..3), right clear.
        for row in 0..4 {
            assert_eq!(out[row * 2], 0xF0, "lo plane row {row}");
            assert_eq!(out[row * 2 + 1], 0xF0, "hi plane row {row}");
        }
        // Rows 4..7 padded transparent.
        for row in 4..8 {
            assert_eq!(out[row * 2], 0);
            assert_eq!(out[row * 2 + 1], 0);
        }
    }

    #[test]
    fn fit_half_preserves_nonzero_in_2x2() {
        // Only pixel (0,0) set to color 1 in NES tile.
        let mut tile = vec![0u8; 16];
        tile[0] = 0x80; // lo plane row0 bit7
        let out = convert_chr_to_gbc_fit_half(&tile);
        assert_eq!(out[0] & 0x80, 0x80);
        assert_eq!(out[1] & 0x80, 0);
    }

    /// Mirror of runtime `nes_video_fit_compose_quad`: pack four shrunk tiles'
    /// high nibbles into one 8x8 (TL/TR rows 0..3, BL/BR rows 4..7).
    fn fit_compose_quad(tiles: [&[u8; 16]; 4]) -> [u8; 16] {
        let mut out = [0u8; 16];
        for (qi, tile) in tiles.iter().enumerate() {
            let row_base = if qi >= 2 { 4 } else { 0 };
            let right = qi & 1 == 1;
            for row in 0..4 {
                let lo = tile[row * 2] & 0xF0;
                let hi = tile[row * 2 + 1] & 0xF0;
                let (lo, hi) = if right {
                    (lo >> 4, hi >> 4)
                } else {
                    (lo, hi)
                };
                let dest = (row_base + row) * 2;
                out[dest] |= lo;
                out[dest + 1] |= hi;
            }
        }
        out
    }

    #[test]
    fn fit_compose_four_solid_f0_tiles_fills_all_ff() {
        // Only rows 0..3 matter for shrunk content; match convert_chr_to_gbc_fit_half.
        let mut tile = [0u8; 16];
        for row in 0..4 {
            tile[row * 2] = 0xF0;
            tile[row * 2 + 1] = 0xF0;
        }
        let out = fit_compose_quad([&tile, &tile, &tile, &tile]);
        assert_eq!(out, [0xFFu8; 16]);
        // Sanity: solid NES color-3 half CHR also composes to all FF.
        let half = convert_chr_to_gbc_fit_half(&vec![0xFFu8; 16]);
        let mut half_arr = [0u8; 16];
        half_arr.copy_from_slice(&half);
        assert_eq!(fit_compose_quad([&half_arr; 4]), [0xFFu8; 16]);
    }
}
