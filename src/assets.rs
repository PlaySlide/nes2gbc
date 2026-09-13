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


/// Fit-screen metatile atlas: pack 2x2 shrunk NES tiles into one GBC 8x8 at
/// recompile time. Runtime only binary-searches a ROM lookup — never composes
/// pixels on device.
///
/// Lookup record format (6 bytes, little-endian, sorted by key ascending):
///   t0, t1, t2, t3, idx_lo, idx_hi
/// where (t0,t1,t2,t3) = TL,TR,BL,BR NES tile indices and idx is the atlas
/// tile id (1..=255; 0 is reserved blank). VRAM bank 0 holds at most 256
/// tiles, so the atlas is hard-capped at FIT_ATLAS_MAX_TILES.

pub const FIT_ATLAS_MAX_TILES: usize = 256;
pub const FIT_LOOKUP_RECORD_SIZE: usize = 6;

/// Four NES tile indices forming one BG metatile (TL, TR, BL, BR).
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct MetatileKey {
    pub tiles: [u8; 4],
}

impl MetatileKey {
    pub fn new(t0: u8, t1: u8, t2: u8, t3: u8) -> Self {
        Self {
            tiles: [t0, t1, t2, t3],
        }
    }
}

/// Compose four already-shrunk 8x8 tiles (4x4 useful in top-left) into one
/// full 8x8 GBC tile: TL|TR / BL|BR quadrants.
pub fn compose_metatile_pixels(
    tl: &[[u8; 8]; 8],
    tr: &[[u8; 8]; 8],
    bl: &[[u8; 8]; 8],
    br: &[[u8; 8]; 8],
) -> [[u8; 8]; 8] {
    let mut dst = [[0u8; 8]; 8];
    for r in 0..4 {
        for c in 0..4 {
            dst[r][c] = tl[r][c];
            dst[r][c + 4] = tr[r][c];
            dst[r + 4][c] = bl[r][c];
            dst[r + 4][c + 4] = br[r][c];
        }
    }
    dst
}

fn encode_gbc_tile_array(px: &[[u8; 8]; 8]) -> [u8; 16] {
    let mut out = [0u8; 16];
    let mut i = 0;
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
        out[i] = lo;
        out[i + 1] = hi;
        i += 2;
    }
    out
}

/// Host-side compose of four NES CHR tiles (16 bytes each, NES bitplane order)
/// into one GBC 8x8 metatile (16 bytes).
pub fn compose_metatile_from_nes_tiles(
    tl: &[u8],
    tr: &[u8],
    bl: &[u8],
    br: &[u8],
) -> [u8; 16] {
    let tl_p = shrink_tile_half(tl);
    let tr_p = shrink_tile_half(tr);
    let bl_p = shrink_tile_half(bl);
    let br_p = shrink_tile_half(br);
    encode_gbc_tile_array(&compose_metatile_pixels(&tl_p, &tr_p, &bl_p, &br_p))
}

/// Scan PRG ROM for nametable-like 32-wide row pairs and collect metatile keys
/// with occurrence counts. CHR bank is treated as 0 (NROM / undiscoverable mapper CHR).
///
/// Two passes:
/// 1. Every 32-aligned consecutive row pair (primary frequency signal).
/// 2. Every byte offset as the start of a 32-wide × N view (N>=2): even y/x
///    metatiles. Low extra weight so rare misaligned embeddings can fill spare
///    atlas slots without drowning aligned hits.
pub fn discover_metatile_keys(prg: &[u8]) -> std::collections::BTreeMap<MetatileKey, u32> {
    use std::collections::BTreeMap;
    let mut counts: BTreeMap<MetatileKey, u32> = BTreeMap::new();

    // Pass 1: aligned 32-byte row pairs → 16 metatiles each.
    let mut row_start = 0usize;
    while row_start + 64 <= prg.len() {
        let row0 = &prg[row_start..row_start + 32];
        let row1 = &prg[row_start + 32..row_start + 64];
        for x in (0..32).step_by(2) {
            let key = MetatileKey::new(row0[x], row0[x + 1], row1[x], row1[x + 1]);
            *counts.entry(key).or_default() += 2; // stronger weight for aligned
        }
        row_start += 32;
    }

    // Pass 2: misaligned 32×2 windows at every 8-byte offset (lighter than
    // every-byte). Weight 1 — fills spare atlas slots after aligned hits.
    if prg.len() >= 64 {
        for start in (0..=(prg.len() - 64)).step_by(8) {
            if start % 32 == 0 {
                continue;
            }
            for x in (0..32).step_by(2) {
                let key = MetatileKey::new(
                    prg[start + x],
                    prg[start + x + 1],
                    prg[start + 32 + x],
                    prg[start + 32 + x + 1],
                );
                *counts.entry(key).or_default() += 1;
            }
        }
    }


    counts
}

fn nes_tile_at<'a>(chr: &'a [u8], index: u8) -> &'a [u8] {
    let start = (index as usize) * 16;
    if start + 16 <= chr.len() {
        &chr[start..start + 16]
    } else {
        // Out-of-range tile index → blank
        static BLANK: [u8; 16] = [0; 16];
        &BLANK
    }
}

/// Built fit-screen atlas + sorted lookup blob.
#[derive(Debug, Clone)]
pub struct FitAtlas {
    /// Raw GBC tiles concatenated (16 bytes each). Index 0 is blank.
    pub atlas: Vec<u8>,
    /// Sorted 6-byte records: t0,t1,t2,t3,idx_lo,idx_hi.
    pub lookup: Vec<u8>,
    /// Number of atlas tiles including the reserved blank (atlas.len()/16).
    pub tile_count: usize,
    /// Unique keys discovered before capping.
    pub discovered: usize,
    /// True if discovery exceeded the VRAM cap and frequency pruning ran.
    pub truncated: bool,
}

/// Build a fit-screen metatile atlas from CHR + PRG scan.
/// `max_tiles` includes the reserved blank tile at index 0 (clamped to
/// FIT_ATLAS_MAX_TILES). Multi-bank CHR is not modeled — bank 0 only.
pub fn build_fit_atlas(chr: &[u8], prg: &[u8], max_tiles: usize) -> FitAtlas {
    let max_tiles = max_tiles.clamp(1, FIT_ATLAS_MAX_TILES);
    let counts = discover_metatile_keys(prg);

    // Prefer keys that appeared in the aligned pass (weight starts at 2).
    let mut ranked: Vec<(MetatileKey, u32)> = counts
        .into_iter()
        .filter(|(_, freq)| *freq >= 2)
        .collect();
    let discovered = ranked.len();
    ranked.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0)));

    let usable = max_tiles.saturating_sub(1);
    let truncated = ranked.len() > usable;
    ranked.truncate(usable);

    // Atlas starts with blank tile 0.
    let mut atlas = vec![0u8; 16];
    let mut records: Vec<(MetatileKey, u16)> = Vec::with_capacity(ranked.len());

    for (key, _freq) in ranked {
        let idx = (atlas.len() / 16) as u16;
        let tile = compose_metatile_from_nes_tiles(
            nes_tile_at(chr, key.tiles[0]),
            nes_tile_at(chr, key.tiles[1]),
            nes_tile_at(chr, key.tiles[2]),
            nes_tile_at(chr, key.tiles[3]),
        );
        atlas.extend_from_slice(&tile);
        records.push((key, idx));
    }

    records.sort_by(|a, b| a.0.cmp(&b.0));

    let mut lookup = Vec::with_capacity(records.len() * FIT_LOOKUP_RECORD_SIZE);
    for (key, idx) in records {
        lookup.extend_from_slice(&key.tiles);
        lookup.push((idx & 0xFF) as u8);
        lookup.push((idx >> 8) as u8);
    }

    let tile_count = atlas.len() / 16;
    // Pad to full VRAM bank (256 tiles) so runtime can blit $1000 safely.
    atlas.resize(FIT_ATLAS_MAX_TILES * 16, 0);

    FitAtlas {
        atlas,
        lookup,
        tile_count,
        discovered,
        truncated,
    }
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

    #[test]
    fn compose_four_solid_shrunk_tiles_fills_gbc_tile() {
        // Solid color-3 NES tile → 4x4 solid after shrink.
        let solid = vec![0xFFu8; 16];
        let out = compose_metatile_from_nes_tiles(&solid, &solid, &solid, &solid);
        assert_eq!(out, [0xFFu8; 16]);
    }

    #[test]
    fn compose_places_quadrants() {
        // TL only color-1 at pixel (0,0) of NES tile → shrunk (0,0).
        let mut tl = vec![0u8; 16];
        tl[0] = 0x80;
        let empty = vec![0u8; 16];
        let out = compose_metatile_from_nes_tiles(&tl, &empty, &empty, &empty);
        // GBC row0 lo plane bit7 set
        assert_eq!(out[0] & 0x80, 0x80);
        assert_eq!(out[1] & 0x80, 0);
        // Right half of row0 clear
        assert_eq!(out[0] & 0x0F, 0);
    }

    #[test]
    fn discover_aligned_row_pair_yields_metatiles() {
        let mut prg = vec![0u8; 64];
        // One metatile at (0,0): tiles 1,2 / 3,4
        prg[0] = 1;
        prg[1] = 2;
        prg[32] = 3;
        prg[33] = 4;
        let counts = discover_metatile_keys(&prg);
        assert!(counts.contains_key(&MetatileKey::new(1, 2, 3, 4)));
    }

    #[test]
    fn build_atlas_reserves_blank_and_sorts_lookup() {
        let mut chr = vec![0u8; 256 * 16];
        // Make tiles 1..4 solid
        for t in 1..=4u8 {
            let start = t as usize * 16;
            chr[start..start + 16].fill(0xFF);
        }
        let mut prg = vec![0u8; 64];
        prg[0] = 1;
        prg[1] = 2;
        prg[32] = 3;
        prg[33] = 4;
        let atlas = build_fit_atlas(&chr, &prg, 256);
        assert_eq!(&atlas.atlas[..16], &[0u8; 16]);
        assert!(atlas.tile_count >= 2);
        assert_eq!(atlas.lookup.len() % FIT_LOOKUP_RECORD_SIZE, 0);
        // Lookup sorted
        let records: Vec<_> = atlas.lookup.chunks(6).collect();
        for w in records.windows(2) {
            assert!(w[0][..4] <= w[1][..4]);
        }
        // Our key present with non-zero index
        let hit = records.iter().find(|r| r[..4] == [1, 2, 3, 4]);
        assert!(hit.is_some());
        let idx = u16::from_le_bytes([hit.unwrap()[4], hit.unwrap()[5]]);
        assert!(idx >= 1);
        assert_eq!(&atlas.atlas[idx as usize * 16..][..16], &[0xFFu8; 16]);
    }
}
