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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn interleaves_nes_bitplanes_by_row() {
        let input: Vec<u8> = (0u8..16).collect();
        let output = convert_chr_to_gbc(&input);
        assert_eq!(output, vec![0,8,1,9,2,10,3,11,4,12,5,13,6,14,7,15]);
    }
}

/// FIT_SCREEN: shrink every NES tile to a 4x4 image in the top-left quarter
/// of an 8x8 CGB tile (rows 4-7 and columns 4-7 stay transparent).
/// Rows pair up (row a=2r over b=2r+1: a's opaque pixels win) and so do
/// columns (the even/left pixel wins if opaque). The runtime's CHR-RAM path
/// (`nes_fit_shrink_tile`) implements exactly the same rule.
pub fn fit_shrink_tile(tile: &[u8]) -> [u8; 16] {
    let mut out = [0u8; 16];
    for r in 0..4 {
        let (a, b) = (2 * r, 2 * r + 1);
        let na = tile[a] | tile[a + 8];
        let lo = tile[a] | (tile[b] & !na);
        let hi = tile[a + 8] | (tile[b + 8] & !na);
        let n = lo | hi;
        let keep = |x: u8| (x & 0xAA) | ((x & 0x55 & !(n >> 1)) << 1);
        out[2 * r] = fit_pack(keep(lo));
        out[2 * r + 1] = fit_pack(keep(hi));
    }
    out
}

/// Bits 7,5,3,1 -> bits 7,6,5,4 (the runtime's 256-byte nibble table).
pub fn fit_pack(x: u8) -> u8 {
    ((x & 0x80) | ((x & 0x20) << 1) | ((x & 0x08) << 2) | ((x & 0x02) << 3)) & 0xF0
}

pub fn convert_chr_to_gbc_fit(chr: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(chr.len());
    for tile in chr.chunks(16) {
        if tile.len() < 16 {
            out.extend(std::iter::repeat(0).take(tile.len()));
            continue;
        }
        out.extend_from_slice(&fit_shrink_tile(tile));
    }
    out
}
