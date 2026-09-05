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
