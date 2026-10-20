//! Emission helpers shared by the plain (recompile.rs) and superblock
//! (state_superblock.rs) translators. Stage 1 of docs/IR_REWRITE.md moves
//! duplicated logic here; behaviour and output are unchanged.

use crate::mir::MirBuf;

use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::fmt::Write;

use crate::{
    cfg::{ControlFlowGraph, EdgeKind},
    ir::{Flag, IrOp},
};

pub(crate) const DISPATCH_BANK_START: u16 = 32;
pub(crate) const CODE_BANK_START: u16 = 40;
pub(crate) const ESTIMATED_BANK_BUDGET: usize = 0x3000;

pub(crate) fn estimated_block_size(block: &crate::cfg::BasicBlock) -> usize {
    64 + block.instructions.len() * 96
}

pub(crate) fn emit_pc_dispatch(out: &mut MirBuf, target: u16) {
    out.ins("ld", &["hl", format!("${target:04X}").as_str()], None);
    out.ins("jp", &["nes_dispatch_hl"], None);
}

pub(crate) fn flag_mask(flag: Flag) -> u8 {
    match flag {
        Flag::Carry => 0x01,
        Flag::Zero => 0x02,
        Flag::InterruptDisable => 0x04,
        Flag::Decimal => 0x08,
        Flag::Overflow => 0x40,
        Flag::Negative => 0x80,
    }
}


pub(crate) fn is_branch(m: crate::cpu6502::Mnemonic) -> bool {
    use crate::cpu6502::Mnemonic::*;
    matches!(m, Bcc|Bcs|Beq|Bmi|Bne|Bpl|Bvc|Bvs)
}

pub(crate) fn terminal_mnemonic(m: crate::cpu6502::Mnemonic) -> bool {
    use crate::cpu6502::Mnemonic::*;
    matches!(m, Bcc|Bcs|Beq|Bmi|Bne|Bpl|Bvc|Bvs|Jmp|Jsr|Rts|Rti|Brk)
}

pub(crate) fn select_reachable(graph: &ControlFlowGraph, reset: u16, limit: usize) -> BTreeSet<u16> {
    let mut selected = BTreeSet::new();
    let mut queue = VecDeque::from([reset]);
    for &entry in &graph.entry_points {
        if entry != reset {
            queue.push_back(entry);
        }
    }

    while let Some(addr) = queue.pop_front() {
        if selected.len() >= limit || !selected.insert(addr) {
            continue;
        }
        let Some(block) = graph.blocks.get(&addr) else { continue };

        for target in block.edges.iter().filter_map(|edge| edge.target) {
            if graph.blocks.contains_key(&target) && !selected.contains(&target) {
                queue.push_back(target);
            }
        }
    }

    selected
}

/// `max_bank`: last MBC5 bank code may occupy (the superblock path keeps
/// bank 255 for runtime helpers).
pub(crate) fn assign_code_banks(
    graph: &ControlFlowGraph,
    selected: &BTreeSet<u16>,
    max_bank: u16,
) -> BTreeMap<u16, u16> {
    let mut assigned = BTreeMap::new();
    let mut bank = CODE_BANK_START;
    let mut used = 0usize;

    for addr in selected {
        let block = graph.blocks.get(addr).expect("selected block must exist");
        let cost = estimated_block_size(block);
        assert!(
            cost <= ESTIMATED_BANK_BUDGET,
            "basic block ${addr:04X} is too large for conservative bank packing"
        );

        if used != 0 && used + cost > ESTIMATED_BANK_BUDGET {
            bank += 1;
            used = 0;
        }

        assert!(bank <= max_bank, "translated code exceeds 8-bit MBC5 bank allocator (max bank {max_bank})");
        assigned.insert(*addr, bank);
        used += cost;
    }

    assigned
}

pub(crate) fn emit_dispatch_tables(out: &mut MirBuf, selected: &BTreeSet<u16>) {
    for segment in 0u16..8 {
        let bank = DISPATCH_BANK_START + segment;
        let base = 0x8000u16 + segment * 0x1000;

        writeln!(
            out,
            "SECTION \"NES dispatch table {segment}\", ROMX[$4000], BANK[{bank}]"
        )
        .unwrap();

        let mut cursor = 0usize;
        let addresses: Vec<u16> = if segment == 7 {
            selected.range(base..=0xFFFF).copied().collect()
        } else {
            let end = base + 0x1000;
            selected.range(base..end).copied().collect()
        };
        for addr in addresses {
            let offset = (addr - base) as usize;
            if offset > cursor {
                writeln!(out, "    ds {}, $00", (offset - cursor) * 4).unwrap();
            }

            writeln!(out, "    db BANK(nes_{addr:04X}), $00").unwrap();
            writeln!(out, "    dw nes_{addr:04X}").unwrap();
            cursor = offset + 1;
        }

        if cursor < 0x1000 {
            writeln!(out, "    ds {}, $00", (0x1000 - cursor) * 4).unwrap();
        }
        writeln!(out).unwrap();
    }
}

pub(crate) fn approx_code_bytes(asm: &str) -> usize {
    // Overestimate so backward `jr` range checks stay conservative.
    asm.lines()
        .map(str::trim)
        .filter(|l| {
            !l.is_empty()
                && !l.starts_with(';')
                && !l.starts_with("IF ")
                && !l.starts_with("ELSE")
                && !l.starts_with("ENDC")
                && !l.starts_with("SECTION")
                && !l.ends_with(':')
        })
        .map(|_| 2usize)
        .sum()
}

pub(crate) fn emit_static_target(
    out: &mut MirBuf,
    target: u16,
    current_bank: u16,
    banks: &BTreeMap<u16, u16>,
    section_offs: Option<&BTreeMap<u16, usize>>,
    section_pc: usize,
) -> bool {
    match banks.get(&target).copied() {
        Some(bank) if bank == current_bank => {
            if let Some(offs) = section_offs {
                if let Some(&target_off) = offs.get(&target) {
                    // `jr` is 2 bytes; offset is from the following instruction.
                    let delta = target_off as isize - (section_pc + 2) as isize;
                    if (-128..128).contains(&delta) {
                        out.ins("jr", &[format!("nes_{target:04X}").as_str()], None);
                        return true;
                    }
                }
            }
            out.ins("jp", &[format!("nes_{target:04X}").as_str()], None);
            true
        }
        Some(bank) => {
            out.ins("ld", &["a", format!("${bank:02X}").as_str()], None);
            out.ins("ld", &["hl", format!("nes_{target:04X}").as_str()], None);
            out.ins("jp", &["nes_jump_known_hl_a"], None);
            true
        }
        None => false,
    }
}

pub(crate) fn reachable_from_roots(
    graph: &ControlFlowGraph,
    selected: &BTreeSet<u16>,
    roots: &[u16],
) -> BTreeSet<u16> {
    let mut seen = BTreeSet::new();
    let mut queue = VecDeque::new();
    for &root in roots {
        if selected.contains(&root) {
            queue.push_back(root);
        }
    }

    while let Some(addr) = queue.pop_front() {
        if !seen.insert(addr) {
            continue;
        }
        let Some(block) = graph.blocks.get(&addr) else { continue };
        for target in block.edges.iter().filter_map(|edge| edge.target) {
            if selected.contains(&target) && !seen.contains(&target) {
                queue.push_back(target);
            }
        }
    }
    seen
}

pub(crate) fn nmi_exclusive_blocks(
    graph: &ControlFlowGraph,
    selected: &BTreeSet<u16>,
    reset: u16,
    nmi: u16,
    irq: u16,
) -> BTreeSet<u16> {
    let nmi_reach = reachable_from_roots(graph, selected, &[nmi]);
    let mut other_roots = vec![reset];
    if irq != reset && irq != nmi {
        other_roots.push(irq);
    }
    let other_reach = reachable_from_roots(graph, selected, &other_roots);

    // If ordinary/IRQ control reaches an unresolved computed jump, it could
    // enter any translated block. Refuse to prove exclusivity in that case.
    let unsafe_dynamic = other_reach.iter().any(|addr| {
        graph.blocks.get(addr).is_some_and(|block| {
            block.edges.iter().any(|edge| {
                matches!(edge.kind, EdgeKind::IndirectJump { .. }) && edge.target.is_none()
            })
        })
    });
    if unsafe_dynamic {
        return BTreeSet::new();
    }

    nmi_reach.difference(&other_reach).copied().collect()
}

pub(crate) fn emit_known_target(
    out: &mut MirBuf,
    target: u16,
    current_bank: u16,
    banks: &BTreeMap<u16, u16>,
    section_offs: Option<&BTreeMap<u16, usize>>,
    section_pc: usize,
) {
    if !emit_static_target(out, target, current_bank, banks, section_offs, section_pc) {
        emit_pc_dispatch(out, target);
    }
}

pub(crate) fn emit_static_control(
    out: &mut MirBuf,
    ops: &[IrOp],
    current_bank: u16,
    banks: &BTreeMap<u16, u16>,
    section_offs: Option<&BTreeMap<u16, usize>>,
    section_pc: usize,
    fast_a_target: Option<u16>,
) -> bool {
    if ops.len() != 1 {
        return false;
    }

    match ops[0] {
        IrOp::Branch { flag, when, target } if banks.contains_key(&target) => {
            match flag {
                Flag::Carry => {
                    out.ins("ldh", &["a", "[nes_c_shadow]"], None);
                    out.ins("and", &["a"], None);
                    out.ins("jr", &[format!("{}", if when { "z" } else { "nz" }).as_str(), ":+"], None);
                }
                Flag::Zero => {
                    out.ins("ldh", &["a", "[nes_z_shadow]"], None);
                    out.ins("and", &["a"], None);
                    out.ins("jr", &[format!("{}", if when { "nz" } else { "z" }).as_str(), ":+"], None);
                }
                Flag::Negative => {
                    out.ins("ldh", &["a", "[nes_n_shadow]"], None);
                    out.ins("bit", &["7", "a"], None);
                    out.ins("jr", &[format!("{}", if when { "z" } else { "nz" }).as_str(), ":+"], None);
                }
                _ => {
                    out.ins("ldh", &["a", "[nes_p]"], None);
                    out.ins("and", &[format!("${:02X}", flag_mask(flag)).as_str()], None);
                    out.ins("jr", &[format!("{}", if when { "z" } else { "nz" }).as_str(), ":+"], None);
                }
            }
            emit_static_target(out, target, current_bank, banks, section_offs, section_pc);
            out.label("");
            true
        }
        IrOp::Jump(target) if banks.contains_key(&target) => {
            if fast_a_target == Some(target) {
                emit_fast_a_target(out, target, current_bank, banks)
            } else {
                emit_static_target(out, target, current_bank, banks, section_offs, section_pc)
            }
        }
        IrOp::Call { target, return_addr } if banks.contains_key(&target) => {
            out.ins("ld", &["hl", format!("${return_addr:04X}").as_str()], None);
            out.ins("call", &["nes_stack_push_return_hl"], None);
            emit_static_target(out, target, current_bank, banks, section_offs, section_pc);
            true
        }
        _ => false,
    }
}

pub(crate) fn emit_fast_a_target(
    out: &mut MirBuf,
    target: u16,
    current_bank: u16,
    banks: &BTreeMap<u16, u16>,
) -> bool {
    if banks.get(&target).copied() != Some(current_bank) {
        return false;
    }

    // PROFILE_TRACE expects every canonical block entry to run its trace hook,
    // so instrumented builds deliberately use the canonical entry. Release
    // builds jump past the redundant nes_a reload and keep host A resident.
    writeln!(out, "IF DEF(NES2GBC_PROFILE_TRACE)").unwrap();
    out.ins("jp", &[format!("nes_{target:04X}").as_str()], None);
    writeln!(out, "ELSE").unwrap();
    out.ins("jp", &[format!("nes_{target:04X}_fast_a").as_str()], None);
    writeln!(out, "ENDC").unwrap();

    // The bank-locality repacker only understands canonical nes_XXXX labels.
    // Keep a compile-time-dead canonical edge so its must-link analysis keeps
    // the source and the private fast entry in the same ROM bank.
    writeln!(out, "IF 0").unwrap();
    out.ins("jp", &[format!("nes_{target:04X}").as_str()], Some("A-residency bank-locality relation"));
    writeln!(out, "ENDC").unwrap();
    true
}
