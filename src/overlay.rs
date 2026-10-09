//! Ahead-of-time translation of 6502 code that a banked game copies from
//! PRG ROM into cartridge PRG RAM ($6000-$7FFF) and then executes there
//! (Zelda copies bank 1 $A500.. to $6C90-$7EFF).
//!
//! Detection is static and generic: a constant pointer setup
//! (`LDA #lo / STA zp ...`) followed by a `LDA (src),Y / STA (dst),Y` copy
//! whose destination is in $6000-$7FFF and whose source is PRG ROM. The
//! copied image is analysed from every static ROM transfer into it. Each
//! translated block re-checks its own bytes in RAM at entry and falls back
//! to the interpreter on any mismatch, so a wrong guess (different copy,
//! self-modifying code, data) only costs speed, never correctness.

use std::collections::{BTreeMap, BTreeSet, VecDeque};

use crate::cfg::{BasicBlock, ControlFlowGraph, Edge, EdgeKind};
use crate::cpu6502::{self, AddressingMode, Mnemonic};

#[derive(Debug, Clone)]
pub struct RamOverlay {
    /// First RAM address of the copied image.
    pub dst: u16,
    /// Expected RAM contents starting at `dst`.
    pub bytes: Vec<u8>,
    /// Translated blocks, keyed by RAM PC.
    pub blocks: BTreeMap<u16, BasicBlock>,
}

impl RamOverlay {
    pub fn byte(&self, pc: u16) -> Option<u8> {
        let o = pc.checked_sub(self.dst)? as usize;
        self.bytes.get(o).copied()
    }
    fn end(&self) -> u32 {
        self.dst as u32 + self.bytes.len() as u32
    }
}

/// `LDA (src),Y / STA (dst),Y` with both pointers known constants: `(dst, src)`
/// when it copies PRG ROM into PRG RAM.
fn pointer_copy(
    l: &crate::cpu6502::DecodedInstruction,
    s: &crate::cpu6502::DecodedInstruction,
    zp: &BTreeMap<u8, u8>,
) -> Option<(u16, u16)> {
    if l.def.mnemonic != Mnemonic::Lda || l.def.mode != AddressingMode::IndirectIndexed
        || s.def.mnemonic != Mnemonic::Sta || s.def.mode != AddressingMode::IndirectIndexed
    {
        return None;
    }
    let get = |p: u8| -> Option<u16> {
        Some(*zp.get(&p)? as u16 | (*zp.get(&p.wrapping_add(1))? as u16) << 8)
    };
    let (src, dst) = (get(l.operand as u8)?, get(s.operand as u8)?);
    ((0x6000..0x8000).contains(&dst) && src >= 0x8000).then_some((dst, src))
}

/// `(view bank, dst, src)` for each constant ROM->PRG-RAM pointer copy.
fn copy_candidates(views: &[(u8, ControlFlowGraph)]) -> Vec<(u8, u16, u16)> {
    let mut out = Vec::new();
    for (bank, g) in views {
        for block in g.blocks.values() {
            // Constant zero-page bytes written by this block.
            let (mut a, mut x, mut y): (Option<u8>, Option<u8>, Option<u8>) = (None, None, None);
            let mut zp: BTreeMap<u8, u8> = BTreeMap::new();
            for (k, ins) in block.instructions.iter().enumerate() {
                if let Some(next) = block.instructions.get(k + 1) {
                    if let Some(c) = pointer_copy(ins, next, &zp) {
                        out.push((*bank, c.0, c.1));
                    }
                }
                let m = ins.def.mnemonic;
                let mode = ins.def.mode;
                match (m, mode) {
                    (Mnemonic::Lda, AddressingMode::Immediate) => a = Some(ins.operand as u8),
                    (Mnemonic::Ldx, AddressingMode::Immediate) => x = Some(ins.operand as u8),
                    (Mnemonic::Ldy, AddressingMode::Immediate) => y = Some(ins.operand as u8),
                    (Mnemonic::Sta | Mnemonic::Stx | Mnemonic::Sty, AddressingMode::ZeroPage) => {
                        let v = match m { Mnemonic::Sta => a, Mnemonic::Stx => x, _ => y };
                        match v {
                            Some(v) => { zp.insert(ins.operand as u8, v); }
                            None => { zp.remove(&(ins.operand as u8)); }
                        }
                    }
                    (Mnemonic::Clc | Mnemonic::Sec | Mnemonic::Cld | Mnemonic::Sei | Mnemonic::Cli, _) => {}
                    (Mnemonic::Tax, _) => x = a,
                    (Mnemonic::Tay, _) => y = a,
                    (Mnemonic::Txa, _) => a = x,
                    (Mnemonic::Tya, _) => a = y,
                    _ => {
                        // Anything else may clobber registers/zero page.
                        a = None; x = None; y = None;
                        if !matches!(mode, AddressingMode::Immediate | AddressingMode::Implied | AddressingMode::Accumulator | AddressingMode::Relative)
                            && !matches!(m, Mnemonic::Lda | Mnemonic::Ldx | Mnemonic::Ldy | Mnemonic::Cmp | Mnemonic::Cpx | Mnemonic::Cpy | Mnemonic::Bit | Mnemonic::Adc | Mnemonic::Sbc | Mnemonic::And | Mnemonic::Ora | Mnemonic::Eor)
                        {
                            zp.clear();
                        }
                    }
                }
            }
            if zp.len() < 4 {
                continue;
            }
            // The copy loop is this block's fallthrough/jump successor.
            for edge in &block.edges {
                if !matches!(edge.kind, EdgeKind::Fallthrough | EdgeKind::Jump) {
                    continue;
                }
                let Some(t) = edge.target else { continue };
                let Some(lp) = g.blocks.get(&t) else { continue };
                for w in lp.instructions.windows(2) {
                    if let Some(c) = pointer_copy(&w[0], &w[1], &zp) {
                        out.push((*bank, c.0, c.1));
                    }
                }
            }
        }
    }
    out.sort();
    out.dedup();
    out
}

fn discover(ov: &RamOverlay, entries: &BTreeSet<u16>) -> BTreeMap<u16, BasicBlock> {
    let inside = |pc: u16| (pc as u32) >= ov.dst as u32 && (pc as u32) < ov.end();
    let mut work: VecDeque<u16> = entries.iter().copied().filter(|&p| inside(p)).collect();
    let mut seen: BTreeSet<u16> = work.iter().copied().collect();
    let mut blocks = BTreeMap::new();
    let push = |t: u16, work: &mut VecDeque<u16>, seen: &mut BTreeSet<u16>| {
        if inside(t) && seen.insert(t) {
            work.push_back(t);
        }
    };
    while let Some(start) = work.pop_front() {
        let mut pc = start;
        let mut ins = Vec::new();
        let mut edges = Vec::new();
        let mut complete = false;
        loop {
            if pc != start && seen.contains(&pc) {
                edges.push(Edge { kind: EdgeKind::Fallthrough, target: Some(pc) });
                complete = true;
                break;
            }
            let mut raw = [0u8; 3];
            let mut ok = true;
            for (k, b) in raw.iter_mut().enumerate() {
                match ov.byte(pc.wrapping_add(k as u16)) {
                    Some(v) => *b = v,
                    None if k == 0 => ok = false,
                    None => {}
                }
            }
            if !ok {
                break;
            }
            let Ok(i) = cpu6502::decode(pc, &raw) else { break };
            let next = pc.wrapping_add(i.def.len() as u16);
            if !inside(next.wrapping_sub(1)) {
                break;
            }
            ins.push(i);
            use Mnemonic::*;
            match i.def.mnemonic {
                Bcc | Bcs | Beq | Bmi | Bne | Bpl | Bvc | Bvs => {
                    let t = next.wrapping_add((i.operand as u8 as i8) as i16 as u16);
                    edges.push(Edge { kind: EdgeKind::BranchTaken, target: Some(t) });
                    edges.push(Edge { kind: EdgeKind::Fallthrough, target: Some(next) });
                    push(t, &mut work, &mut seen);
                    push(next, &mut work, &mut seen);
                    complete = true;
                }
                Jsr => {
                    edges.push(Edge { kind: EdgeKind::Call, target: Some(i.operand) });
                    edges.push(Edge { kind: EdgeKind::CallReturn, target: Some(next) });
                    push(i.operand, &mut work, &mut seen);
                    push(next, &mut work, &mut seen);
                    complete = true;
                }
                Jmp if i.def.mode == AddressingMode::Absolute => {
                    edges.push(Edge { kind: EdgeKind::Jump, target: Some(i.operand) });
                    push(i.operand, &mut work, &mut seen);
                    complete = true;
                }
                Jmp => {
                    edges.push(Edge { kind: EdgeKind::IndirectJump { pointer: i.operand }, target: None });
                    complete = true;
                }
                Rts | Rti | Brk => complete = true,
                _ => {
                    pc = next;
                    continue;
                }
            }
            break;
        }
        if !complete || ins.is_empty() {
            continue;
        }
        // A block that stores into its own bytes would run stale translated
        // code after the store; leave such blocks to the interpreter.
        let lo = start as u32;
        let hi = ins.last().map(|i| i.pc as u32 + i.def.len() as u32).unwrap_or(lo);
        let self_modifying = ins.iter().any(|i| {
            let writes = matches!(i.def.mnemonic, Mnemonic::Sta | Mnemonic::Stx | Mnemonic::Sty | Mnemonic::Inc | Mnemonic::Dec | Mnemonic::Asl | Mnemonic::Lsr | Mnemonic::Rol | Mnemonic::Ror)
                && i.def.mode != AddressingMode::Accumulator;
            if !writes {
                return false;
            }
            match i.def.mode {
                AddressingMode::ZeroPage | AddressingMode::ZeroPageX | AddressingMode::ZeroPageY => false,
                AddressingMode::Absolute => (lo..hi).contains(&(i.operand as u32)),
                AddressingMode::AbsoluteX | AddressingMode::AbsoluteY => {
                    let b = i.operand as u32;
                    b < hi && b + 0xFF >= lo
                }
                _ => true, // indirect: could hit anything
            }
        });
        if self_modifying {
            continue;
        }
        blocks.insert(start, BasicBlock { start, instructions: ins, edges });
    }
    blocks
}

/// Detect a ROM->PRG-RAM code copy and translate the copied image. Returns
/// `None` when no copy is found or nothing in it is reachable statically.
pub fn detect(views: &[(u8, ControlFlowGraph)], prg: &[u8]) -> Option<RamOverlay> {
    let bank_count = prg.len() / 0x4000;
    let cands = copy_candidates(views);
    // Static ROM transfers into PRG RAM: the overlay's entry points.
    let mut entries = BTreeSet::new();
    for (_, g) in views {
        for block in g.blocks.values() {
            for e in &block.edges {
                if let Some(t) = e.target {
                    if (0x6000..0x8000).contains(&t) {
                        entries.insert(t);
                    }
                }
            }
        }
    }
    if std::env::var_os("NES2GBC_CFG_DEBUG").is_some() {
        eprintln!("ram-overlay: copies {:04X?} entries {:04X?}", cands, entries);
        for (bank, g) in views {
            for b in g.blocks.values() {
                if b.instructions.windows(2).any(|w| w[0].def.mode == AddressingMode::IndirectIndexed && w[0].def.mnemonic == Mnemonic::Lda && w[1].def.mnemonic == Mnemonic::Sta && w[1].def.mode == AddressingMode::IndirectIndexed) {
                    let preds: Vec<u16> = g.blocks.values().filter(|p| p.edges.iter().any(|e| e.target == Some(b.start))).map(|p| p.start).collect();
                    eprintln!("  copy loop bank {bank} @{:04X} preds {:04X?}", b.start, preds);
                }
            }
        }
    }
    let mut best: Option<RamOverlay> = None;
    for (bank, dst, src) in cands {
        let (phys, window_end) = if src >= 0xC000 { (bank_count - 1, 0x10000u32) } else { (bank as usize, 0xC000u32) };
        let len = (0x8000u32 - dst as u32).min(window_end - src as u32) as usize;
        let base = phys * 0x4000 + (src as usize & 0x3FFF);
        let bytes = prg[base..base + len].to_vec();
        let mut ov = RamOverlay { dst, bytes, blocks: BTreeMap::new() };
        ov.blocks = discover(&ov, &entries);
        if ov.blocks.is_empty() {
            continue;
        }
        if best.as_ref().map_or(true, |b| ov.blocks.len() > b.blocks.len()) {
            best = Some(ov);
        }
    }
    best
}
