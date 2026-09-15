use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::fmt::Write;

use crate::{
    cfg::{BasicBlock, ControlFlowGraph, EdgeKind},
    ir::{self, Flag, IrOp, ModifyTarget, Operand, Register, StackValue},
    lr35902,
};

#[derive(Debug, Clone, Copy)]
pub struct EmitOptions {
    pub reset: u16,
    pub max_blocks: Option<usize>,
    pub debug_trace: bool,
}

impl Default for EmitOptions {
    fn default() -> Self {
        Self { reset: 0x8000, max_blocks: Some(64), debug_trace: false }
    }
}

fn is_branch(m: crate::cpu6502::Mnemonic) -> bool {
    use crate::cpu6502::Mnemonic::*;
    matches!(m, Bcc|Bcs|Beq|Bmi|Bne|Bpl|Bvc|Bvs)
}

fn terminal_mnemonic(m: crate::cpu6502::Mnemonic) -> bool {
    use crate::cpu6502::Mnemonic::*;
    matches!(m, Bcc|Bcs|Beq|Bmi|Bne|Bpl|Bvc|Bvs|Jmp|Jsr|Rts|Rti|Brk)
}

fn select_reachable(graph: &ControlFlowGraph, reset: u16, limit: usize) -> BTreeSet<u16> {
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

const DISPATCH_BANK_START: u16 = 32;
const CODE_BANK_START: u16 = 40;
const ESTIMATED_BANK_BUDGET: usize = 0x3000;

fn estimated_block_size(block: &crate::cfg::BasicBlock) -> usize {
    64 + block.instructions.len() * 96
}

fn assign_code_banks(
    graph: &ControlFlowGraph,
    selected: &BTreeSet<u16>,
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

        assert!(bank <= 255, "translated code exceeds current 8-bit MBC5 bank allocator");
        assigned.insert(*addr, bank);
        used += cost;
    }

    assigned
}

fn emit_dispatch_tables(out: &mut String, selected: &BTreeSet<u16>) {
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

fn emit_pc_dispatch(out: &mut String, target: u16) {
    writeln!(out, "    ld hl, ${target:04X}").unwrap();
    writeln!(out, "    jp nes_dispatch_hl").unwrap();
}

fn flag_mask(flag: Flag) -> u8 {
    match flag {
        Flag::Carry => 0x01,
        Flag::Zero => 0x02,
        Flag::InterruptDisable => 0x04,
        Flag::Decimal => 0x08,
        Flag::Overflow => 0x40,
        Flag::Negative => 0x80,
    }
}

fn approx_code_bytes(asm: &str) -> usize {
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

fn emit_static_target(
    out: &mut String,
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
                        writeln!(out, "    jr nes_{target:04X}").unwrap();
                        return true;
                    }
                }
            }
            writeln!(out, "    jp nes_{target:04X}").unwrap();
            true
        }
        Some(bank) => {
            writeln!(out, "    ld a, ${bank:02X}").unwrap();
            writeln!(out, "    ld hl, nes_{target:04X}").unwrap();
            writeln!(out, "    jp nes_jump_known_hl_a").unwrap();
            true
        }
        None => false,
    }
}

fn emit_fast_a_target(
    out: &mut String,
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
    writeln!(out, "    jp nes_{target:04X}").unwrap();
    writeln!(out, "ELSE").unwrap();
    writeln!(out, "    jp nes_{target:04X}_fast_a").unwrap();
    writeln!(out, "ENDC").unwrap();

    // The bank-locality repacker only understands canonical nes_XXXX labels.
    // Keep a compile-time-dead canonical edge so its must-link analysis keeps
    // the source and the private fast entry in the same ROM bank.
    writeln!(out, "IF 0").unwrap();
    writeln!(out, "    jp nes_{target:04X} ; A-residency bank-locality relation").unwrap();
    writeln!(out, "ENDC").unwrap();
    true
}

fn emit_static_control(
    out: &mut String,
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
                    writeln!(out, "    ldh a, [nes_c_shadow]").unwrap();
                    writeln!(out, "    and a").unwrap();
                    writeln!(out, "    jr {}, :+", if when { "z" } else { "nz" }).unwrap();
                }
                Flag::Zero => {
                    writeln!(out, "    ldh a, [nes_z_shadow]").unwrap();
                    writeln!(out, "    and a").unwrap();
                    writeln!(out, "    jr {}, :+", if when { "nz" } else { "z" }).unwrap();
                }
                Flag::Negative => {
                    writeln!(out, "    ldh a, [nes_n_shadow]").unwrap();
                    writeln!(out, "    bit 7, a").unwrap();
                    writeln!(out, "    jr {}, :+", if when { "z" } else { "nz" }).unwrap();
                }
                _ => {
                    writeln!(out, "    ldh a, [nes_p]").unwrap();
                    writeln!(out, "    and ${:02X}", flag_mask(flag)).unwrap();
                    writeln!(out, "    jr {}, :+", if when { "z" } else { "nz" }).unwrap();
                }
            }
            emit_static_target(out, target, current_bank, banks, section_offs, section_pc);
            writeln!(out, ":").unwrap();
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
            writeln!(out, "    ld hl, ${return_addr:04X}").unwrap();
            writeln!(out, "    call nes_stack_push_return_hl").unwrap();
            emit_static_target(out, target, current_bank, banks, section_offs, section_pc);
            true
        }
        _ => false,
    }
}

fn emit_known_target(
    out: &mut String,
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

fn nmi_poll_points(graph: &ControlFlowGraph, selected: &BTreeSet<u16>) -> BTreeSet<u16> {
    let mut points = BTreeSet::new();

    for &entry in &graph.entry_points {
        if selected.contains(&entry) {
            points.insert(entry);
        }
    }

    for (&start, block) in &graph.blocks {
        if !selected.contains(&start) {
            continue;
        }

        for target in block.edges.iter().filter_map(|edge| edge.target) {
            if selected.contains(&target) && target <= start {
                points.insert(target);
            }
        }
    }

    points
}

fn reachable_from_roots(
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

fn nmi_exclusive_blocks(
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

fn is_static_control_ir(ops: &[IrOp], banks: &BTreeMap<u16, u16>) -> bool {
    if ops.len() != 1 {
        return false;
    }
    match ops[0] {
        IrOp::Branch { target, .. }
        | IrOp::Jump(target)
        | IrOp::Call { target, .. } => banks.contains_key(&target),
        _ => false,
    }
}

fn pending_ops_before_static_control(
    block: &BasicBlock,
    banks: &BTreeMap<u16, u16>,
) -> Option<Vec<IrOp>> {
    let mut pending = Vec::new();
    for instruction in &block.instructions {
        let ops = ir::lower_instruction(*instruction).ok()?;
        if is_static_control_ir(&ops, banks) {
            break;
        }
        pending.extend(ops);
    }
    Some(pending)
}

fn direct_store_keeps_a(dst: Operand) -> bool {
    matches!(dst, Operand::ZeroPage(_))
        || matches!(dst, Operand::Absolute(addr) if addr < 0x2000)
}

fn ops_exit_with_clean_a(ops: &[IrOp]) -> bool {
    let mut live = false;
    for op in ops {
        live = match *op {
            IrOp::SetFlag { .. } => false,
            IrOp::Load { dst, .. } => dst == Register::A,
            IrOp::Store { src, dst } => src == Register::A && direct_store_keeps_a(dst),
            IrOp::Transfer { src, dst, .. } => src == Register::A || dst == Register::A,
            IrOp::Inc(reg) | IrOp::Dec(reg) => reg == Register::A,
            IrOp::Logic { .. } | IrOp::Arithmetic { .. } => true,
            IrOp::Modify { target, .. } => target == ModifyTarget::Accumulator,
            IrOp::Bit { .. } | IrOp::Compare { .. } => false,
            IrOp::StackPush(_) => false,
            IrOp::StackPop(StackValue::A) => true,
            IrOp::StackPop(StackValue::Status) => false,
            IrOp::ReadIo { dst, .. } => dst == Register::A,
            IrOp::WriteIo { addr: 0x4011, src } => src == Register::A,
            IrOp::WriteIo { .. } => false,
            IrOp::Nop => live,
            IrOp::Branch { .. }
            | IrOp::Jump(_)
            | IrOp::JumpIndirect { .. }
            | IrOp::Call { .. }
            | IrOp::Return
            | IrOp::ReturnInterrupt
            | IrOp::Break { .. } => false,
        };
    }
    live
}

fn seeded_a_asm(ops: &[IrOp]) -> Option<String> {
    let asm = lr35902::emit_ops(ops);
    let needle = "    ldh a, [nes_a]\n";
    let pos = asm.find(needle)?;
    let prefix = &asm[..pos];

    // Keep the first experiment deliberately narrow. The only allowed work
    // before the canonical A reload is immediate setup in E (CMP #imm). It
    // does not touch A or host flags used by the translated instruction.
    if prefix.lines().any(|line| {
        let c = line.trim();
        !c.is_empty() && !c.starts_with(';') && !c.starts_with("ld e, $")
    }) {
        return None;
    }

    let mut out = String::with_capacity(asm.len() - needle.len());
    out.push_str(prefix);
    out.push_str(&asm[pos + needle.len()..]);
    Some(out)
}

fn unconditional_static_successor(block: &BasicBlock) -> Option<(u16, bool)> {
    let last = block.instructions.last()?;
    if last.def.mnemonic == crate::cpu6502::Mnemonic::Jmp {
        let target = block
            .edges
            .iter()
            .find(|edge| matches!(edge.kind, EdgeKind::Jump))
            .and_then(|edge| edge.target)?;
        return Some((target, true));
    }

    if !terminal_mnemonic(last.def.mnemonic) {
        let target = block
            .edges
            .iter()
            .find(|edge| matches!(edge.kind, EdgeKind::Fallthrough))
            .and_then(|edge| edge.target)?;
        return Some((target, false));
    }
    None
}

fn clean_a_resident_edges(
    graph: &ControlFlowGraph,
    selected_list: &[u16],
    banks: &BTreeMap<u16, u16>,
    poll_points: &BTreeSet<u16>,
    nmi_exclusive: &BTreeSet<u16>,
) -> (BTreeMap<u16, u16>, BTreeSet<u16>) {
    let selected: BTreeSet<u16> = selected_list.iter().copied().collect();
    let positions: BTreeMap<u16, usize> = selected_list
        .iter()
        .copied()
        .enumerate()
        .map(|(i, addr)| (addr, i))
        .collect();

    let mut seedable = BTreeSet::new();
    for &addr in selected_list {
        let Some(block) = graph.blocks.get(&addr) else { continue };
        let Some(ops) = pending_ops_before_static_control(block, banks) else { continue };
        if ops.is_empty() || seeded_a_asm(&ops).is_none() {
            continue;
        }
        if !poll_points.contains(&addr) || nmi_exclusive.contains(&addr) {
            seedable.insert(addr);
        }
    }

    let mut edges = BTreeMap::new();
    let mut targets = BTreeSet::new();
    for &src in selected_list {
        let Some(block) = graph.blocks.get(&src) else { continue };
        let Some((target, is_jump)) = unconditional_static_successor(block) else { continue };
        if !selected.contains(&target) || !seedable.contains(&target) {
            continue;
        }
        if banks.get(&src) != banks.get(&target) {
            continue;
        }

        // A literal adjacent fallthrough already costs zero transfer cycles;
        // forcing a JP merely to skip a 3-M-cycle HRAM load would be slower.
        if !is_jump {
            let next = positions.get(&src).and_then(|i| selected_list.get(i + 1)).copied();
            if next == Some(target) {
                continue;
            }
        }

        let Some(ops) = pending_ops_before_static_control(block, banks) else { continue };
        if !ops_exit_with_clean_a(&ops) {
            continue;
        }
        edges.insert(src, target);
        targets.insert(target);
    }
    (edges, targets)
}

fn emit_cfg_impl(
    graph: &ControlFlowGraph,
    options: EmitOptions,
    interrupt_entries: Option<(u16, u16)>,
) -> String {
    let mut out = String::new();
    writeln!(out, "; Generated by nes2gbc").unwrap();
    writeln!(out, "; Native banked LR35902 output").unwrap();
    writeln!(out).unwrap();

    let limit = options.max_blocks.unwrap_or(graph.blocks.len());
    let selected = select_reachable(graph, options.reset, limit);
    let banks = assign_code_banks(graph, &selected);
    let poll_points = nmi_poll_points(graph, &selected);
    let nmi_exclusive = interrupt_entries
        .map(|(nmi, irq)| nmi_exclusive_blocks(graph, &selected, options.reset, nmi, irq))
        .unwrap_or_default();
    let selected_list: Vec<u16> = selected.iter().copied().collect();

    let (fast_a_edges, fast_a_targets) = if interrupt_entries.is_some() && !options.debug_trace {
        clean_a_resident_edges(
            graph,
            &selected_list,
            &banks,
            &poll_points,
            &nmi_exclusive,
        )
    } else {
        (BTreeMap::new(), BTreeSet::new())
    };

    if interrupt_entries.is_some() {
        println!(
            "a-residency: kept clean 6502 A live across {} same-bank static edge(s) / {} private target entrie(s); {} block(s) proven NMI-exclusive",
            fast_a_edges.len(),
            fast_a_targets.len(),
            nmi_exclusive.len()
        );
    }

    writeln!(out, "SECTION \"Generated NES reset entry\", ROM0").unwrap();
    writeln!(out, "nes_reset:").unwrap();
    writeln!(out, "    ld a, [nes_reset_count]").unwrap();
    writeln!(out, "    inc a").unwrap();
    writeln!(out, "    ld [nes_reset_count], a").unwrap();
    emit_pc_dispatch(&mut out, options.reset);
    writeln!(out).unwrap();

    // Defer fallthrough jumps so consecutive same-bank blocks can share a
    // SECTION and fall through in place instead of paying `jp nes_XXXX`.
    let mut pending_fallthrough: Option<(u16 /*target*/, u16 /*from_bank*/)> = None;
    let mut section_offs: BTreeMap<u16, usize> = BTreeMap::new();
    let mut section_pc: usize = 0;

    for (idx, addr) in selected_list.iter().copied().enumerate() {
        let block = graph.blocks.get(&addr).expect("selected block must exist");
        let bank = banks[&addr];

        let continue_fallthrough = matches!(
            pending_fallthrough,
            Some((target, from_bank)) if target == addr && from_bank == bank
        );
        if continue_fallthrough {
            pending_fallthrough = None;
        } else {
            if let Some((target, from_bank)) = pending_fallthrough.take() {
                // Section is closing; range for jr no longer matters after reset.
                emit_known_target(
                    &mut out,
                    target,
                    from_bank,
                    &banks,
                    Some(&section_offs),
                    section_pc,
                );
                writeln!(out).unwrap();
            }
            writeln!(
                out,
                "SECTION \"NES block {addr:04X}\", ROMX, BANK[{bank}]"
            )
            .unwrap();
            section_offs.clear();
            section_pc = 0;
        }

        section_offs.insert(block.start, section_pc);
        writeln!(out, "nes_{:04X}:", block.start).unwrap();

        writeln!(out, "IF DEF(NES2GBC_PROFILE_TRACE)").unwrap();
        writeln!(out, "    ld hl, ${:04X}", block.start).unwrap();
        writeln!(out, "    call nes_profile_trace_pc").unwrap();
        writeln!(out, "ENDC").unwrap();
        // Profile-trace body is stripped in release; do not charge section_pc.

        if options.debug_trace {
            // Keep an exact current NES-PC breadcrumb even when optimized direct
            // jumps bypass the dynamic dispatcher.
            let before = out.len();
            writeln!(out, "    ld a, ${:02X}", (block.start >> 8) as u8).unwrap();
            writeln!(out, "    ld [nes_debug_pc_hi], a").unwrap();
            writeln!(out, "    ld a, ${:02X}", block.start as u8).unwrap();
            writeln!(out, "    ld [nes_debug_pc_lo], a").unwrap();
            section_pc += approx_code_bytes(&out[before..]);
        }

        if poll_points.contains(&block.start) {
            let exclusive = nmi_exclusive.contains(&block.start);
            if exclusive {
                // Keep the exact poll text visible to the conservative generated-
                // ASM analyses as a liveness/call barrier, but emit zero machine
                // code. Nested NES NMI delivery is impossible while translated
                // NMI is active. The late elision pass may remove this dead text.
                writeln!(out, "    ; NMI-exclusive safe-point retained as analysis barrier").unwrap();
                writeln!(out, "IF 0").unwrap();
            }

            // Usually there is no pending frame, so loop safe-points pay only
            // an HRAM byte test instead of a helper call and live LY polling.
            let before = out.len();
            writeln!(out, "    ldh a, [nes_host_vblank_pending]").unwrap();
            writeln!(out, "    and a").unwrap();
            writeln!(out, "    jr z, :+").unwrap();
            writeln!(out, "    ld hl, ${:04X}", block.start).unwrap();
            writeln!(out, "    call nes_poll_nmi_hl").unwrap();
            writeln!(out, "    and a").unwrap();
            writeln!(out, "    jp nz, nes_nmi_entry").unwrap();
            writeln!(out, ":").unwrap();
            section_pc += approx_code_bytes(&out[before..]);
            if exclusive {
                writeln!(out, "ENDC").unwrap();
            }
        }

        let seed_a_target = fast_a_targets.contains(&block.start);
        let mut seed_a_pending = seed_a_target;
        if seed_a_target {
            // Canonical/dynamic entries materialize host A exactly once. Proven
            // static predecessors enter at the private label with the same clean
            // architectural value already resident in A.
            let before = out.len();
            writeln!(out, "    ldh a, [nes_a]").unwrap();
            writeln!(out, "nes_{:04X}_fast_a:", block.start).unwrap();
            section_pc += approx_code_bytes(&out[before..]);
        }

        let mut pending: Vec<IrOp> = Vec::new();
        let write_insn_comment = |out: &mut String, instruction: &crate::cpu6502::DecodedInstruction| {
            writeln!(
                out,
                "    ; ${:04X}: ${:02X} {:?} {:?}",
                instruction.pc,
                instruction.opcode,
                instruction.def.mnemonic,
                instruction.def.mode
            )
            .unwrap();
        };

        let emit_pending = |out: &mut String, pending: &[IrOp], seed: &mut bool| {
            if *seed {
                let asm = seeded_a_asm(pending)
                    .expect("A-resident target must have the precomputed seedable first batch");
                out.push_str(&asm);
                *seed = false;
            } else {
                out.push_str(&lr35902::emit_ops(pending));
            }
        };

        for instruction in &block.instructions {
            match ir::lower_instruction(*instruction) {
                Ok(ops) => {
                    // Probe without writing: static control (branch/jmp) clobbers A.
                    let mut probe = String::new();
                    if emit_static_control(&mut probe, &ops, bank, &banks, None, 0, None) {
                        if !pending.is_empty() {
                            let before = out.len();
                            emit_pending(&mut out, &pending, &mut seed_a_pending);
                            section_pc += approx_code_bytes(&out[before..]);
                            pending.clear();
                        }
                        write_insn_comment(&mut out, instruction);
                        let before = out.len();
                        let fast_target = fast_a_edges.get(&block.start).copied();
                        let _ = emit_static_control(
                            &mut out,
                            &ops,
                            bank,
                            &banks,
                            Some(&section_offs),
                            section_pc,
                            fast_target,
                        );
                        section_pc += approx_code_bytes(&out[before..]);
                    } else {
                        write_insn_comment(&mut out, instruction);
                        pending.extend(ops);
                    }
                }
                Err(err) => {
                    if !pending.is_empty() {
                        let before = out.len();
                        emit_pending(&mut out, &pending, &mut seed_a_pending);
                        section_pc += approx_code_bytes(&out[before..]);
                        pending.clear();
                    }
                    write_insn_comment(&mut out, instruction);
                    let before = out.len();
                    writeln!(out, "    ; TODO {err}").unwrap();
                    writeln!(out, "    ld a, ${:02X}", instruction.pc as u8).unwrap();
                    writeln!(out, "    ldh [nes_fault_pc_lo], a").unwrap();
                    writeln!(out, "    ld a, ${:02X}", (instruction.pc >> 8) as u8).unwrap();
                    writeln!(out, "    ldh [nes_fault_pc_hi], a").unwrap();
                    writeln!(out, "    jp nes_unimplemented").unwrap();
                    section_pc += approx_code_bytes(&out[before..]);
                    break;
                }
            }
        }
        if !pending.is_empty() {
            let before = out.len();
            emit_pending(&mut out, &pending, &mut seed_a_pending);
            section_pc += approx_code_bytes(&out[before..]);
            pending.clear();
        }
        assert!(!seed_a_pending, "A-resident target emitted no seedable body batch");

        if let Some(last) = block.instructions.last() {
            if is_branch(last.def.mnemonic) || !terminal_mnemonic(last.def.mnemonic) {
                if let Some(target) = block
                    .edges
                    .iter()
                    .find(|edge| matches!(edge.kind, EdgeKind::Fallthrough))
                    .and_then(|edge| edge.target)
                {
                    let next = selected_list.get(idx + 1).copied();
                    if next == Some(target) && banks.get(&target) == Some(&bank) {
                        // Next emitted block is this fallthrough and shares our
                        // bank — keep the section open and fall through.
                        pending_fallthrough = Some((target, bank));
                    } else {
                        let before = out.len();
                        if fast_a_edges.get(&block.start).copied() == Some(target) {
                            let _ = emit_fast_a_target(&mut out, target, bank, &banks);
                        } else {
                            emit_known_target(
                                &mut out,
                                target,
                                bank,
                                &banks,
                                Some(&section_offs),
                                section_pc,
                            );
                        }
                        section_pc += approx_code_bytes(&out[before..]);
                    }
                }
            }
        }
        writeln!(out).unwrap();
    }

    if let Some((target, from_bank)) = pending_fallthrough.take() {
        let before = out.len();
        emit_known_target(
            &mut out,
            target,
            from_bank,
            &banks,
            Some(&section_offs),
            section_pc,
        );
        let _ = approx_code_bytes(&out[before..]);
        writeln!(out).unwrap();
    }

    emit_dispatch_tables(&mut out, &selected);
    out
}

pub fn emit_cfg(graph: &ControlFlowGraph, options: EmitOptions) -> String {
    emit_cfg_impl(graph, options, None)
}

pub fn emit_cfg_with_interrupts(
    graph: &ControlFlowGraph,
    options: EmitOptions,
    nmi: u16,
    irq: u16,
) -> String {
    emit_cfg_impl(graph, options, Some((nmi, irq)))
}

#[derive(Debug, Clone)]
pub struct RuntimeConfig<'a> {
    pub mapper: u16,
    pub mirroring: crate::ines::Mirroring,
    pub prg_len: usize,
    pub chr_len: usize,
    pub nmi: u16,
    pub irq: u16,
    pub prg_file: &'a str,
    pub chr_file: &'a str,
    pub chr_gbc_file: &'a str,
}

pub fn emit_runtime_config(config: &RuntimeConfig<'_>) -> String {
    let mut out = String::new();
    let mirroring = match config.mirroring {
        crate::ines::Mirroring::Horizontal => 0,
        crate::ines::Mirroring::Vertical => 1,
        crate::ines::Mirroring::FourScreen => 2,
    };
    let prg_16k_mirror = if config.prg_len == 0x4000 { 1usize } else { 0usize };
    let chr_banks_8k = ((config.chr_len + 0x1FFF) / 0x2000).max(1);
    let chr_mask = chr_banks_8k.next_power_of_two() - 1;

    writeln!(out, "; Cartridge/runtime metadata").unwrap();
    writeln!(out, "SECTION \"Generated runtime metadata\", ROM0").unwrap();
    writeln!(out, "nes_generated_init:").unwrap();
    writeln!(out, "    ld a, ${:02X}", config.mapper as u8).unwrap();
    writeln!(out, "    ld [nes_mapper], a").unwrap();
    writeln!(out, "    ld a, ${mirroring:02X}").unwrap();
    writeln!(out, "    ld [nes_mirroring], a").unwrap();
    writeln!(out, "    ld a, ${prg_16k_mirror:02X}").unwrap();
    writeln!(out, "    ld [nes_prg_16k_mirror], a").unwrap();
    writeln!(out, "    ld a, ${:02X}", chr_mask as u8).unwrap();
    writeln!(out, "    ld [nes_chr_bank_mask], a").unwrap();
    writeln!(out, "    ld a, ${:02X}", (3 + chr_banks_8k) as u8).unwrap();
    writeln!(out, "    ld [nes_chr_gbc_bank_base], a").unwrap();
    writeln!(out, "    xor a").unwrap();
    writeln!(out, "    ld [nes_chr_bank], a").unwrap();
    if prg_16k_mirror != 0 {
        writeln!(out, "    call nes_cache_prg16_to_wram").unwrap();
    }
    writeln!(out, "    ret").unwrap();
    writeln!(out).unwrap();

    writeln!(out, "nes_nmi_entry:").unwrap();
    writeln!(out, "    ld hl, ${:04X}", config.nmi).unwrap();
    writeln!(out, "    jp nes_dispatch_hl").unwrap();
    writeln!(out, "nes_irq_entry:").unwrap();
    writeln!(out, "    ld hl, ${:04X}", config.irq).unwrap();
    writeln!(out, "    jp nes_dispatch_hl").unwrap();
    writeln!(out).unwrap();

    if config.prg_len == 0x4000 {
        writeln!(out, "SECTION \"NES PRG data 0\", ROMX[$4000], BANK[1]").unwrap();
        writeln!(out, "    INCBIN \"{}\", 0, $4000", config.prg_file).unwrap();
    } else {
        for bank in 0..((config.prg_len + 0x3FFF) / 0x4000) {
            let start = bank * 0x4000;
            let len = (config.prg_len - start).min(0x4000);
            writeln!(out, "SECTION \"NES PRG data {bank}\", ROMX[$4000], BANK[{}]", bank + 1).unwrap();
            writeln!(out, "    INCBIN \"{}\", ${start:04X}, ${len:04X}", config.prg_file).unwrap();
        }
    }
    writeln!(out).unwrap();

    let chr_bank_base = 3usize;
    for bank in 0..chr_banks_8k {
        let start = bank * 0x2000;
        let len = if config.chr_len == 0 { 0 } else { (config.chr_len - start).min(0x2000) };
        writeln!(out, "SECTION \"NES CHR data {bank}\", ROMX[$4000], BANK[{}]", chr_bank_base + bank).unwrap();
        if len == 0 {
            writeln!(out, "    ds $2000, 0").unwrap();
        } else {
            writeln!(out, "    INCBIN \"{}\", ${start:04X}, ${len:04X}", config.chr_file).unwrap();
        }
    }
    writeln!(out).unwrap();
    let chr_gbc_bank_base = chr_bank_base + chr_banks_8k;
    for bank in 0..chr_banks_8k {
        let start = bank * 0x2000;
        let len = if config.chr_len == 0 { 0 } else { (config.chr_len - start).min(0x2000) };
        writeln!(out, "SECTION \"GBC converted CHR {bank}\", ROMX[$4000], BANK[{}]", chr_gbc_bank_base + bank).unwrap();
        if len == 0 {
            writeln!(out, "    ds $2000, 0").unwrap();
        } else {
            writeln!(out, "    INCBIN \"{}\", ${start:04X}, ${len:04X}", config.chr_gbc_file).unwrap();
        }
    }

    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cfg;

    #[test]
    fn nmi_poll_points_include_backward_loop_targets() {
        let mut prg = vec![0xEA; 0x8000];
        prg[0..6].copy_from_slice(&[0xA2, 0x03, 0xCA, 0xD0, 0xFD, 0x60]);
        let graph = cfg::discover(0, &prg, &[0x8000]).unwrap();
        let selected: BTreeSet<u16> = graph.blocks.keys().copied().collect();
        let points = nmi_poll_points(&graph, &selected);
        assert!(points.contains(&0x8002));
    }

    #[test]
    fn runtime_config_caches_mirrored_16k_prg() {
        let cfg = RuntimeConfig {
            mapper: 0,
            mirroring: crate::ines::Mirroring::Horizontal,
            prg_len: 0x4000,
            chr_len: 0x2000,
            nmi: 0xC000,
            irq: 0xC000,
            prg_file: "test.prg.bin",
            chr_file: "test.chr.bin",
            chr_gbc_file: "test.chr.gbc.bin",
        };
        let asm = emit_runtime_config(&cfg);
        assert!(asm.contains("call nes_cache_prg16_to_wram"));
    }

    #[test]
    fn reset_entry_dispatches_into_banked_code() {
        let mut prg = vec![0xEA; 0x8000];
        prg[0x1000] = 0x60;
        let graph = cfg::discover(0, &prg, &[0x9000]).unwrap();
        let asm = emit_cfg(&graph, EmitOptions { reset: 0x9000, max_blocks: Some(1), debug_trace: false });
        assert!(asm.contains("SECTION \"Generated NES reset entry\", ROM0"));
        assert!(asm.contains("ld hl, $9000"));
        assert!(asm.contains("jp nes_dispatch_hl"));
        assert!(asm.contains("SECTION \"NES block 9000\", ROMX, BANK[40]"));
    }

    #[test]
    fn static_carry_branches_use_lazy_shadow() {
        let mut prg = vec![0xEA; 0x8000];
        prg[0..4].copy_from_slice(&[0x38, 0xB0, 0x00, 0x60]);
        let graph = cfg::discover(0, &prg, &[0x8000]).unwrap();
        let asm = emit_cfg(&graph, EmitOptions { reset: 0x8000, max_blocks: Some(4), debug_trace: false });
        assert!(asm.contains("ldh a, [nes_c_shadow]"));
    }

    #[test]
    fn static_nz_branches_use_lazy_status_shadows() {
        let mut prg = vec![0xEA; 0x8000];
        prg[0..6].copy_from_slice(&[0xA9, 0x00, 0xF0, 0x01, 0x60, 0x60]);
        let graph = cfg::discover(0, &prg, &[0x8000]).unwrap();
        let asm = emit_cfg(&graph, EmitOptions { reset: 0x8000, max_blocks: Some(4), debug_trace: false });
        assert!(asm.contains("ldh a, [nes_z_shadow]"));
        assert!(asm.contains("and a"));
    }

    #[test]
    fn branch_not_taken_uses_pc_dispatch() {
        let mut prg = vec![0xEA; 0x8000];
        prg[0..5].copy_from_slice(&[0xD0, 0x02, 0x60, 0xEA, 0x60]);
        let graph = cfg::discover(0, &prg, &[0x8000]).unwrap();
        let asm = emit_cfg(&graph, EmitOptions { reset: 0x8000, max_blocks: Some(3), debug_trace: false });
        assert!(asm.contains("jp nes_8004") || asm.contains("jp nes_8002"));
    }

    #[test]
    fn dispatch_table_handles_ffff_without_u16_overflow() {
        let mut prg = vec![0xEA; 0x8000];
        prg[0x7FFF] = 0x60;
        let graph = cfg::discover(0, &prg, &[0xFFFF]).unwrap();
        let asm = emit_cfg(&graph, EmitOptions { reset: 0xFFFF, max_blocks: Some(1), debug_trace: false });
        assert!(asm.contains("nes_FFFF:"));
        assert!(asm.contains("BANK(nes_FFFF)"));
    }

    #[test]
    fn dispatch_table_points_at_selected_block_bank() {
        let mut prg = vec![0xEA; 0x8000];
        prg[0] = 0x60;
        let graph = cfg::discover(0, &prg, &[0x8000]).unwrap();
        let asm = emit_cfg(&graph, EmitOptions { reset: 0x8000, max_blocks: Some(1), debug_trace: false });
        assert!(asm.contains("SECTION \"NES dispatch table 0\", ROMX[$4000], BANK[32]"));
        assert!(asm.contains("db BANK(nes_8000), $00"));
        assert!(asm.contains("dw nes_8000"));
    }

    #[test]
    fn fallthrough_chains_share_section_without_jp() {
        // LDA #$00 / BEQ +1 / RTS / RTS → $8000 falls through into $8004.
        let mut prg = vec![0xEA; 0x8000];
        prg[0..6].copy_from_slice(&[0xA9, 0x00, 0xF0, 0x01, 0x60, 0x60]);
        let graph = cfg::discover(0, &prg, &[0x8000]).unwrap();
        let asm = emit_cfg(
            &graph,
            EmitOptions {
                reset: 0x8000,
                max_blocks: Some(8),
                debug_trace: false,
            },
        );
        assert!(asm.contains("SECTION \"NES block 8000\", ROMX, BANK["));
        assert!(asm.contains("nes_8004:"));
        assert!(!asm.contains("SECTION \"NES block 8004\""));
        assert!(!asm.contains("jp nes_8004"));
    }

    #[test]
    fn clean_a_residency_uses_private_same_bank_entry() {
        let mut prg = vec![0xEA; 0x8000];
        // LDA #$12 / JMP $8010 ... target begins with STA $00.
        prg[0..5].copy_from_slice(&[0xA9, 0x12, 0x4C, 0x10, 0x80]);
        prg[0x10..0x13].copy_from_slice(&[0x85, 0x00, 0x60]);
        let graph = cfg::discover(0, &prg, &[0x8000]).unwrap();
        let asm = emit_cfg_with_interrupts(
            &graph,
            EmitOptions { reset: 0x8000, max_blocks: Some(8), debug_trace: false },
            0x8000,
            0x8000,
        );
        assert!(asm.contains("nes_8010_fast_a:"));
        assert!(asm.contains("jp nes_8010_fast_a"));
    }
}