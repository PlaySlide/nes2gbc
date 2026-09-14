//! CHR asset conversion for the GBC host.
//!
//! `convert_chr_to_gbc` only reorders NES bitplanes into GBC row format.
//! `convert_chr_to_gbc_fit_wide` scales each 8x8 NES tile to a 5x4 crumb.
//! The runtime places those crumbs at X*5/8, Y/2, producing a 160x120 4:3
//! presentation from the NES 256x240 raster while preserving 12px letterbox
//! bars vertically. Content is parked top-left in an 8x8 GBC sprite tile.

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

/// Box sample used by fit scaling: prefer a non-zero pixel so one-pixel
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

/// Build-time 5x4 CHR crumbs for the 160x120 fit-screen mode.
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
    fn fit_wide_keeps_tile_size_and_collapses_to_top_left() {
        // Solid color-3 tile.
        let tile = vec![0xFFu8; 16];
        let out = convert_chr_to_gbc_fit_wide(&tile);
        assert_eq!(out.len(), 16);
        // Rows 0..3: five left pixels solid, three padded transparent.
        for row in 0..4 {
            assert_eq!(out[row * 2], 0xF8, "lo plane row {row}");
            assert_eq!(out[row * 2 + 1], 0xF8, "hi plane row {row}");
        }
        // Rows 4..7 padded transparent.
        for row in 4..8 {
            assert_eq!(out[row * 2], 0);
            assert_eq!(out[row * 2 + 1], 0);
        }
    }

    #[test]
    fn fit_wide_preserves_nonzero_in_box() {
        // Only pixel (0,0) set to color 1 in NES tile.
        let mut tile = vec![0u8; 16];
        tile[0] = 0x80; // lo plane row0 bit7
        let out = convert_chr_to_gbc_fit_wide(&tile);
        assert_eq!(out[0] & 0x80, 0x80);
        assert_eq!(out[1] & 0x80, 0);
    }
}
