//! Heuristic extra CFG entry points for banked ROMs: code that is only reached through data the static
//! discovery cannot follow (cooperative-scheduler task tables, RTS-trick tables, routines called from other
//! unreachable routines). Such code otherwise runs in the slow interpreter. Candidates are `JSR/JMP abs` sites and
//! word tables whose target(s) decode as plausible 6502 code (official opcodes only, ends in RTS/RTI/JMP).
//! Wrong guesses only cost ROM space: the discovery tolerates undecodable bytes. Opt-out: NES2GBC_NO_AUTO_SEEDS=1.
use crate::cpu6502::{decode, AddressingMode as M, Mnemonic as O};

/// `view` is a 32 KiB image mapped at $8000.
fn plausible(view: &[u8], start: u16) -> bool {
    plausible_min(view, start, 2)
}

fn plausible_min(view: &[u8], start: u16, min_n: usize) -> bool {
    if start < 0x8000 {
        return false;
    }
    let mut pc = start as usize - 0x8000;
    let mut n = 0;
    while n < 64 && pc < view.len() {
        let Ok(i) = decode(pc as u16 + 0x8000, &view[pc..]) else { return false };
        n += 1;
        match i.def.mnemonic {
            O::Brk => return false,
            O::Jsr | O::Jmp if i.def.mode == M::Absolute => {
                if !(i.operand >= 0x8000 || (0x6000..0x8000).contains(&i.operand)) {
                    return false;
                }
                if i.def.mnemonic == O::Jmp {
                    return n >= min_n;
                }
            }
            O::Jmp | O::Rts | O::Rti => return n >= min_n,
            O::Bcc | O::Bcs | O::Beq | O::Bmi | O::Bne | O::Bpl | O::Bvc | O::Bvs => {
                let t = (pc as i32 + 2 + (i.operand as u8 as i8) as i32) as i64;
                if t < 0 || t as usize >= view.len() {
                    return false;
                }
            }
            _ => {}
        }
        pc += i.def.len() as usize;
    }
    false
}

/// Returns (fixed-bank entries >= $C000, low-window entries $8000-$BFFF of this view's bank).
pub fn scan(view: &[u8], refs: &std::collections::BTreeSet<u16>) -> (Vec<u16>, Vec<u16>) {
    let mut fixed = Vec::new();
    let mut low = Vec::new();
    if view.len() != 0x8000 || std::env::var_os("NES2GBC_NO_AUTO_SEEDS").is_some() {
        return (fixed, low);
    }
    let add = |t: u16, fixed: &mut Vec<u16>, low: &mut Vec<u16>, site_low: bool| {
        if t >= 0xC000 {
            fixed.push(t);
        } else if site_low {
            low.push(t);
        }
    };
    for i in 0..view.len() - 2 {
        if view[i] == 0x20 || view[i] == 0x4C {
            let t = u16::from_le_bytes([view[i + 1], view[i + 2]]);
            if t >= 0x8000 && plausible(view, t) && plausible(view, i as u16 + 0x8000) {
                add(t, &mut fixed, &mut low, i < 0x4000);
            }
        }
    }
    // Word tables (>= 4 consecutive pointers, maximal runs): jump tables and RTS-trick (address-1) tables.
    // Entries must decode as substantial, clean code (>= 4 instructions) and not all be the same.
    let strong = |w: u16, adj: u16| w >= 0x8000 && plausible_min(view, w.wrapping_add(adj), 4);
    let mut budget = 400usize;
    for i in 0..view.len() - 1 {
        for adj in [0u16, 1] {
            let w0 = u16::from_le_bytes([view[i], view[i + 1]]);
            if !strong(w0, adj) {
                continue;
            }
            if i >= 2 && strong(u16::from_le_bytes([view[i - 2], view[i - 1]]), adj) {
                continue; // not the start of a maximal run
            }
            let mut ws = Vec::new();
            let mut j = i;
            while j + 1 < view.len() {
                let w = u16::from_le_bytes([view[j], view[j + 1]]);
                if !strong(w, adj) {
                    break;
                }
                ws.push(w.wrapping_add(adj));
                j += 2;
            }
            let mut d = ws.clone();
            d.sort_unstable();
            d.dedup();
            // A real table is indexed by code: some discovered `LDA tbl,X/Y` must point into it.
            let lo_addr = 0x8000 + i as u32;
            let hi_addr = lo_addr + 2 * ws.len() as u32;
            let referenced = refs.range(lo_addr as u16..=(hi_addr.min(0xFFFF)) as u16).next().is_some();
            if referenced && ws.len() >= 4 && d.len() >= 3 && d.len() <= budget {
                budget -= d.len();
                for w in d {
                    add(w, &mut fixed, &mut low, i < 0x4000);
                }
            }
        }
    }
    if std::env::var_os("NES2GBC_AUTO_SEEDS_DEBUG").is_some() {
        eprintln!("autoseed fixed={:04X?} low={:04X?}", fixed, low);
    }
    fixed.sort_unstable();
    fixed.dedup();
    low.sort_unstable();
    low.dedup();
    (fixed, low)
}
