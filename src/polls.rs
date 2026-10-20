//! NMI safe-point (poll) placement shared by every translator path:
//! NROM/ROM0 (recompile.rs, state_superblock.rs) and banked views (banked.rs).
use std::collections::BTreeSet;

use crate::cfg::{BasicBlock, ControlFlowGraph};

/// Worst-case NES cycles one counted loop may run without a poll
/// (256 iterations x per-iteration upper bound). 6144 cycles ~= 54 NES
/// scanlines ~= 21% of a frame: the NMI is delayed, never dropped, so the
/// once-per-frame NMI cadence (music tempo, frame counters) is kept.
pub(crate) fn counted_loop_budget() -> u32 {
    std::env::var("NES2GBC_COUNTED_LOOP_BUDGET").ok().and_then(|v| v.parse().ok()).unwrap_or(6144)
}

fn counted_loops_enabled() -> bool {
    std::env::var("NES2GBC_COUNTED_LOOP_POLLS").map(|v| v != "0").unwrap_or(true)
}

fn insn_cycles(i: &crate::cpu6502::DecodedInstruction) -> u32 {
    use crate::cpu6502::{AddressingMode::*, Mnemonic::*};
    let base = match i.def.mode {
        Implied | Accumulator | Immediate => 2,
        ZeroPage | Relative => 3,
        ZeroPageX | ZeroPageY | Absolute => 4,
        AbsoluteX | AbsoluteY => 5,
        Indirect => 5,
        IndexedIndirect | IndirectIndexed => 6,
    };
    let rmw = matches!(i.def.mnemonic, Inc | Dec | Asl | Lsr | Rol | Ror) && i.def.mode != Accumulator;
    let stack = match i.def.mnemonic { Pha | Php => 1, Pla | Plp => 2, _ => 0 };
    base + if rmw { 3 } else { 0 } + stack
}

/// Backward edge S->T that closes a provably counted loop: the region of
/// blocks T..=S has no calls/returns/indirect jumps and no other edge into a
/// region block from a backward position, X or Y is written only by one
/// INX/DEX/INY/DEY in S, and S's branch to T tests Z or N set by that
/// INC/DEC or by CPX/CPY #imm / zp (zp never stored in the region). The
/// counter cycles through all 256 values, so the loop runs <= 256 times.
/// Returns the region exit targets when 256 x the region's cycle bound fits
/// the budget.
pub(crate) fn counted_loop_exits(graph: &ControlFlowGraph, s: u16, t: u16) -> Option<Vec<u16>> {
    use crate::cpu6502::{AddressingMode, Mnemonic::*};
    if !counted_loops_enabled() {
        return None;
    }
    let sb = graph.blocks.get(&s)?;
    let last = sb.instructions.last()?;
    if !matches!(last.def.mnemonic, Bne | Beq | Bpl | Bmi) {
        return None;
    }
    if !sb.edges.iter().any(|e| matches!(e.kind, crate::cfg::EdgeKind::BranchTaken) && e.target == Some(t)) {
        return None;
    }
    let region: Vec<&BasicBlock> = graph.blocks.range(t..=s).map(|(_, b)| b).collect();
    if region.first().map(|b| b.start) != Some(t) {
        return None;
    }
    let mut cycles = 0u32;
    let mut exits = Vec::new();
    let mut zp_stores = BTreeSet::new();
    let mut wild_stores = false;
    let (mut wx, mut wy) = (0u32, 0u32);
    for b in &region {
        for i in &b.instructions {
            cycles += insn_cycles(i);
            match i.def.mnemonic {
                Jsr | Rts | Rti | Brk | Pla | Plp | Txs => return None,
                Jmp if i.def.mode != AddressingMode::Absolute => return None,
                Ldx | Tax | Tsx => wx += 2,
                Inx | Dex => wx += 1,
                Ldy | Tay => wy += 2,
                Iny | Dey => wy += 1,
                _ => {}
            }
            let store = matches!(i.def.mnemonic, Sta | Stx | Sty)
                || (matches!(i.def.mnemonic, Inc | Dec | Asl | Lsr | Rol | Ror) && i.def.mode != AddressingMode::Accumulator);
            if store {
                match i.def.mode {
                    AddressingMode::ZeroPage | AddressingMode::Absolute => { zp_stores.insert(i.operand); }
                    _ => wild_stores = true,
                }
            }
        }
        for e in &b.edges {
            let x = e.target?;
            let inside = graph.blocks.range(t..=s).any(|(&a, _)| a == x);
            if inside {
                if x <= b.start && !(b.start == s && x == t) {
                    return None; // inner loop or second back edge
                }
                if x == t && b.start != s {
                    return None;
                }
            } else if x >= t && x <= s {
                return None; // jumps into the middle of a block
            } else {
                exits.push(x);
            }
        }
    }
    if cycles.saturating_mul(256) > counted_loop_budget() {
        return None;
    }
    // Last Z/N writer before the branch in S.
    let ins = &sb.instructions[..sb.instructions.len() - 1];
    let setter = ins.iter().rev().find(|i| !matches!(i.def.mnemonic,
        Sta | Stx | Sty | Clc | Sec | Cli | Sei | Cld | Sed | Clv | Nop | Pha | Php | Txs | Jmp))?;
    let counter_x = match setter.def.mnemonic {
        Inx | Dex => true,
        Iny | Dey => false,
        Cpx | Cpy => {
            match setter.def.mode {
                AddressingMode::Immediate => {}
                AddressingMode::ZeroPage | AddressingMode::Absolute => {
                    if wild_stores || zp_stores.contains(&setter.operand) {
                        return None;
                    }
                }
                _ => return None,
            }
            setter.def.mnemonic == Cpx
        }
        _ => return None,
    };
    let (w, op) = if counter_x { (wx, [Inx, Dex]) } else { (wy, [Iny, Dey]) };
    if w != 1 || !sb.instructions.iter().any(|i| op.contains(&i.def.mnemonic)) {
        return None;
    }
    exits.sort_unstable();
    exits.dedup();
    Some(exits)
}

/// Exit targets of counted loops whose back edges were left unpolled; each
/// gets a poll so a pending NMI is taken as soon as the loop finishes.
pub(crate) fn counted_loop_exit_polls(graph: &ControlFlowGraph) -> BTreeSet<u16> {
    let mut out = BTreeSet::new();
    for (&s, b) in &graph.blocks {
        for t in b.edges.iter().filter_map(|e| e.target) {
            if t <= s {
                if let Some(ex) = counted_loop_exits(graph, s, t) {
                    out.extend(ex);
                }
            }
        }
    }
    out
}

/// Backward edges (S->T, T<=S) whose target can reach S again inside the
/// view CFG. A backward edge on no cycle cannot repeat without passing a
/// polled point (vector, dynamic entry, or another cyclic back target).
/// None (poll every backward target) under NES2GBC_POLL_ALL_BACK.
pub(crate) fn cyclic_back_targets(graph: &ControlFlowGraph) -> Option<BTreeSet<(u16, u16)>> {
    if std::env::var("NES2GBC_POLL_ALL_BACK").is_ok() {
        return None;
    }
    let mut out = BTreeSet::new();
    for (&start, block) in &graph.blocks {
        for t in block.edges.iter().filter_map(|e| e.target) {
            if t > start {
                continue;
            }
            let mut seen = BTreeSet::new();
            let mut stack = vec![t];
            let mut hit = false;
            while let Some(pc) = stack.pop() {
                if pc == start { hit = true; break; }
                if !seen.insert(pc) { continue; }
                match graph.blocks.get(&pc) {
                    Some(b) => {
                        for e in &b.edges {
                            match e.target { Some(x) => stack.push(x), None => { hit = true; } }
                        }
                        let returns = b.instructions.last().map_or(false, |i| matches!(i.def.mnemonic, crate::cpu6502::Mnemonic::Rts | crate::cpu6502::Mnemonic::Rti));
                        if b.edges.is_empty() && !returns { hit = true; }
                    }
                    None => { hit = true; }
                }
                if hit { break; }
            }
            if hit && counted_loop_exits(graph, start, t).is_none() { out.insert((start, t)); }
        }
    }
    Some(out)
}

pub(crate) fn nmi_poll_points(graph: &ControlFlowGraph, selected: &BTreeSet<u16>) -> BTreeSet<u16> {
    let mut points = BTreeSet::new();
    for &entry in &graph.entry_points {
        if selected.contains(&entry) {
            points.insert(entry);
        }
    }
    // Backward targets only when the edge lies on a cycle (same rule as banked
    // views): an acyclic backward edge cannot repeat without passing another
    // poll. NES2GBC_NROM_CYCLIC_POLLS=0 (or NES2GBC_POLL_ALL_BACK) polls every
    // backward target as before.
    let cyclic = if std::env::var("NES2GBC_NROM_CYCLIC_POLLS").map(|v| v == "0").unwrap_or(false) {
        None
    } else {
        cyclic_back_targets(graph)
    };
    for (&start, block) in &graph.blocks {
        if !selected.contains(&start) {
            continue;
        }
        for target in block.edges.iter().filter_map(|edge| edge.target) {
            if selected.contains(&target)
                && target <= start
                && cyclic.as_ref().map_or(true, |c| c.contains(&(start, target)))
            {
                points.insert(target);
            }
        }
    }
    if cyclic.is_some() {
        for exit in counted_loop_exit_polls(graph) {
            if selected.contains(&exit) {
                points.insert(exit);
            }
        }
    }
    points
}
