use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::fmt::Write;

use crate::{
    cfg::{ControlFlowGraph, EdgeKind},
    ir::{self, ArithmeticOp, Flag, IrOp, LogicOp, ModifyOp, ModifyTarget, Operand, Register},
    lr35902,
    recompile::EmitOptions,
};

const DISPATCH_BANK_START: u16 = 32;
const CODE_BANK_START: u16 = 40;
const ESTIMATED_BANK_BUDGET: usize = 0x3000;
const NES_RAM_BASE: u16 = 0xC000;

fn is_branch(m: crate::cpu6502::Mnemonic) -> bool {
    use crate::cpu6502::Mnemonic::*;
    matches!(m, Bcc | Bcs | Beq | Bmi | Bne | Bpl | Bvc | Bvs)
}

fn terminal_mnemonic(m: crate::cpu6502::Mnemonic) -> bool {
    use crate::cpu6502::Mnemonic::*;
    matches!(
        m,
        Bcc | Bcs | Beq | Bmi | Bne | Bpl | Bvc | Bvs | Jmp | Jsr | Rts | Rti | Brk
    )
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
        let Some(block) = graph.blocks.get(&addr) else {
            continue;
        };
        for target in block.edges.iter().filter_map(|edge| edge.target) {
            if graph.blocks.contains_key(&target) && !selected.contains(&target) {
                queue.push_back(target);
            }
        }
    }
    selected
}

fn estimated_block_size(block: &crate::cfg::BasicBlock) -> usize {
    64 + block.instructions.len() * 96
}

fn assign_code_banks(graph: &ControlFlowGraph, selected: &BTreeSet<u16>) -> BTreeMap<u16, u16> {
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
        assert!(
            bank <= 255,
            "translated code exceeds current 8-bit MBC5 bank allocator"
        );
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
            selected.range(base..base + 0x1000).copied().collect()
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

fn emit_static_control(
    out: &mut String,
    ops: &[IrOp],
    current_bank: u16,
    banks: &BTreeMap<u16, u16>,
    section_offs: Option<&BTreeMap<u16, usize>>,
    section_pc: usize,
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
            emit_static_target(out, target, current_bank, banks, section_offs, section_pc)
        }
        IrOp::Call {
            target,
            return_addr,
        } if banks.contains_key(&target) => {
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
        let Some(block) = graph.blocks.get(&addr) else {
            continue;
        };
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

#[derive(Debug, Default)]
struct SuperblockPlan {
    order: Vec<u16>,
    next: BTreeMap<u16, u16>,
    multi_block_traces: usize,
    chained_edges: usize,
    nmi_private_chained_edges: usize,
    elided_jumps: usize,
    disabled_for_unresolved_indirect: bool,
}

fn preferred_successor(block: &crate::cfg::BasicBlock) -> Option<(u16, bool)> {
    let last = block.instructions.last()?;
    if last.def.mnemonic == crate::cpu6502::Mnemonic::Jmp {
        let target = block
            .edges
            .iter()
            .find(|edge| matches!(edge.kind, EdgeKind::Jump))
            .and_then(|edge| edge.target)?;
        return Some((target, true));
    }
    if is_branch(last.def.mnemonic) || !terminal_mnemonic(last.def.mnemonic) {
        let target = block
            .edges
            .iter()
            .find(|edge| matches!(edge.kind, EdgeKind::Fallthrough))
            .and_then(|edge| edge.target)?;
        return Some((target, false));
    }
    None
}

fn plan_superblocks(
    graph: &ControlFlowGraph,
    selected: &BTreeSet<u16>,
    banks: &BTreeMap<u16, u16>,
    poll_points: &BTreeSet<u16>,
    nmi_exclusive: &BTreeSet<u16>,
) -> SuperblockPlan {
    let base_order: Vec<u16> = selected.iter().copied().collect();
    let unresolved = selected.iter().any(|addr| {
        graph.blocks.get(addr).is_some_and(|block| {
            block.edges.iter().any(|edge| {
                matches!(edge.kind, EdgeKind::IndirectJump { .. }) && edge.target.is_none()
            })
        })
    });
    if unresolved {
        return SuperblockPlan {
            order: base_order,
            disabled_for_unresolved_indirect: true,
            ..SuperblockPlan::default()
        };
    }

    let mut incoming: BTreeMap<u16, usize> = selected
        .iter()
        .copied()
        .map(|addr| (addr, 0usize))
        .collect();
    for &src in selected {
        let Some(block) = graph.blocks.get(&src) else {
            continue;
        };
        for target in block.edges.iter().filter_map(|edge| edge.target) {
            if let Some(count) = incoming.get_mut(&target) {
                *count += 1;
            }
        }
    }

    let entry_points: BTreeSet<u16> = graph.entry_points.iter().copied().collect();
    let mut claimed = BTreeSet::new();
    let mut plan = SuperblockPlan::default();
    for seed in base_order {
        if claimed.contains(&seed) {
            continue;
        }
        let trace_start = plan.order.len();
        let mut current = seed;
        loop {
            if !claimed.insert(current) {
                break;
            }
            plan.order.push(current);
            let Some(block) = graph.blocks.get(&current) else {
                break;
            };
            let Some((target, is_jump)) = preferred_successor(block) else {
                break;
            };
            let nmi_private_edge =
                nmi_exclusive.contains(&current) && nmi_exclusive.contains(&target);
            if !selected.contains(&target)
                || claimed.contains(&target)
                || banks.get(&current) != banks.get(&target)
                || (!nmi_private_edge && incoming.get(&target).copied().unwrap_or(0) != 1)
                || entry_points.contains(&target)
                || (!nmi_private_edge && poll_points.contains(&target))
            {
                break;
            }
            plan.next.insert(current, target);
            plan.chained_edges += 1;
            if nmi_private_edge {
                plan.nmi_private_chained_edges += 1;
            }
            if is_jump {
                plan.elided_jumps += 1;
            }
            current = target;
        }
        if plan.order.len() - trace_start > 1 {
            plan.multi_block_traces += 1;
        }
    }
    plan
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
struct TraceState {
    a_live: bool,
    a_dirty: bool,
    x_b: bool,
    x_dirty: bool,
    y_c: bool,
    y_dirty: bool,
}

#[derive(Debug, Default)]
struct StateStats {
    a_seed_loads: usize,
    a_reload_avoided: usize,
    a_stores_deferred: usize,
    a_materialized: usize,
    x_seed_loads: usize,
    y_seed_loads: usize,
    x_reload_avoided: usize,
    y_reload_avoided: usize,
    x_stores_deferred: usize,
    y_stores_deferred: usize,
    x_materialized: usize,
    y_materialized: usize,
    x_index_uses: usize,
    y_index_uses: usize,
    fast_ops: usize,
    fast_compares: usize,
    fast_arithmetic: usize,
    fast_shifts: usize,
    fast_rmw_addr_reuse: usize,
    barriers: usize,
    canonical_adapters: usize,
}

fn direct_ram_addr(addr: u16) -> Option<u16> {
    if addr < 0x2000 {
        Some(NES_RAM_BASE + (addr & 0x07FF))
    } else {
        None
    }
}

fn emit_update_nz(out: &mut String) {
    writeln!(out, "    ldh [nes_z_shadow], a").unwrap();
    writeln!(out, "    ldh [nes_n_shadow], a").unwrap();
}

fn ensure_a(out: &mut String, state: &mut TraceState, stats: &mut StateStats) {
    if state.a_live {
        stats.a_reload_avoided += 1;
        return;
    }
    writeln!(out, "    ldh a, [nes_a] ; superblock A cache seed").unwrap();
    state.a_live = true;
    state.a_dirty = false;
    stats.a_seed_loads += 1;
}

fn write_a_resident(state: &mut TraceState, stats: &mut StateStats) {
    state.a_live = true;
    state.a_dirty = true;
    stats.a_stores_deferred += 1;
}

fn discard_a(state: &mut TraceState) {
    state.a_live = false;
    state.a_dirty = false;
}

fn materialize_a(out: &mut String, state: &mut TraceState, stats: &mut StateStats) {
    if !state.a_dirty {
        return;
    }
    debug_assert!(state.a_live);
    writeln!(out, "    ldh [nes_a], a ; superblock materialize A").unwrap();
    state.a_dirty = false;
    stats.a_materialized += 1;
}

fn clobber_a(out: &mut String, state: &mut TraceState, stats: &mut StateStats) {
    materialize_a(out, state, stats);
    state.a_live = false;
}

fn ensure_x(out: &mut String, state: &mut TraceState, stats: &mut StateStats) {
    if state.x_b {
        return;
    }
    writeln!(out, "    ldh a, [nes_x]").unwrap();
    writeln!(out, "    ld b, a ; superblock X cache seed").unwrap();
    state.x_b = true;
    state.x_dirty = false;
    stats.x_seed_loads += 1;
}

fn ensure_y(out: &mut String, state: &mut TraceState, stats: &mut StateStats) {
    if state.y_c {
        return;
    }
    writeln!(out, "    ldh a, [nes_y]").unwrap();
    writeln!(out, "    ld c, a ; superblock Y cache seed").unwrap();
    state.y_c = true;
    state.y_dirty = false;
    stats.y_seed_loads += 1;
}

fn sync_xy(out: &mut String, state: &mut TraceState, stats: &mut StateStats) {
    if state.x_dirty {
        debug_assert!(state.x_b);
        writeln!(out, "    ld a, b ; superblock materialize X").unwrap();
        writeln!(out, "    ldh [nes_x], a").unwrap();
        state.x_dirty = false;
        stats.x_materialized += 1;
    }
    if state.y_dirty {
        debug_assert!(state.y_c);
        writeln!(out, "    ld a, c ; superblock materialize Y").unwrap();
        writeln!(out, "    ldh [nes_y], a").unwrap();
        state.y_dirty = false;
        stats.y_materialized += 1;
    }
}

fn sync_state(out: &mut String, state: &mut TraceState, stats: &mut StateStats) {
    // A must be published before X/Y because their materialization uses host A.
    materialize_a(out, state, stats);
    sync_xy(out, state, stats);
    // Even a clean resident A cannot be trusted after X/Y publication.
    state.a_live = false;
}

fn invalidate_state(state: &mut TraceState) {
    debug_assert!(!state.a_dirty && !state.x_dirty && !state.y_dirty);
    state.a_live = false;
    state.x_b = false;
    state.y_c = false;
}

fn emit_add_a_to_hl(out: &mut String) {
    writeln!(out, "    add l").unwrap();
    writeln!(out, "    ld l, a").unwrap();
    writeln!(out, "    jr nc, :+").unwrap();
    writeln!(out, "    inc h").unwrap();
    writeln!(out, ":").unwrap();
}

fn fast_operand_supported(op: Operand) -> bool {
    match op {
        Operand::Immediate(_) | Operand::ZeroPage(_) => true,
        Operand::Absolute(addr) => direct_ram_addr(addr).is_some(),
        Operand::ZeroPageX(_) | Operand::ZeroPageY(_) => true,
        Operand::AbsoluteX(addr) | Operand::AbsoluteY(addr) => {
            addr < 0x0800 && addr.saturating_add(0x00FF) < 0x0800
        }
        Operand::IndexedIndirect(_) | Operand::IndirectIndexed(_) => false,
    }
}

fn fast_store_supported(op: Operand) -> bool {
    !matches!(op, Operand::Immediate(_)) && fast_operand_supported(op)
}

fn emit_operand_load(
    out: &mut String,
    src: Operand,
    state: &mut TraceState,
    stats: &mut StateStats,
) {
    match src {
        Operand::Immediate(v) => writeln!(out, "    ld a, ${v:02X}").unwrap(),
        Operand::ZeroPage(zp) => {
            writeln!(out, "    ld a, [${:04X}]", NES_RAM_BASE + zp as u16).unwrap();
        }
        Operand::Absolute(addr) => {
            writeln!(out, "    ld a, [${:04X}]", direct_ram_addr(addr).unwrap()).unwrap();
        }
        Operand::ZeroPageX(zp) => {
            ensure_x(out, state, stats);
            writeln!(out, "    ld a, b ; superblock cached X index").unwrap();
            stats.x_reload_avoided += 1;
            stats.x_index_uses += 1;
            writeln!(out, "    add ${zp:02X}").unwrap();
            writeln!(out, "    ld l, a").unwrap();
            writeln!(out, "    ld h, $C0").unwrap();
            writeln!(out, "    ld a, [hl]").unwrap();
        }
        Operand::ZeroPageY(zp) => {
            ensure_y(out, state, stats);
            writeln!(out, "    ld a, c ; superblock cached Y index").unwrap();
            stats.y_reload_avoided += 1;
            stats.y_index_uses += 1;
            writeln!(out, "    add ${zp:02X}").unwrap();
            writeln!(out, "    ld l, a").unwrap();
            writeln!(out, "    ld h, $C0").unwrap();
            writeln!(out, "    ld a, [hl]").unwrap();
        }
        Operand::AbsoluteX(addr) => {
            ensure_x(out, state, stats);
            writeln!(out, "    ld hl, ${:04X}", NES_RAM_BASE + addr).unwrap();
            writeln!(out, "    ld a, b ; superblock cached X index").unwrap();
            stats.x_reload_avoided += 1;
            stats.x_index_uses += 1;
            emit_add_a_to_hl(out);
            writeln!(out, "    ld a, [hl]").unwrap();
        }
        Operand::AbsoluteY(addr) => {
            ensure_y(out, state, stats);
            writeln!(out, "    ld hl, ${:04X}", NES_RAM_BASE + addr).unwrap();
            writeln!(out, "    ld a, c ; superblock cached Y index").unwrap();
            stats.y_reload_avoided += 1;
            stats.y_index_uses += 1;
            emit_add_a_to_hl(out);
            writeln!(out, "    ld a, [hl]").unwrap();
        }
        Operand::IndexedIndirect(_) | Operand::IndirectIndexed(_) => unreachable!(),
    }
}

fn emit_operand_store(
    out: &mut String,
    dst: Operand,
    state: &mut TraceState,
    stats: &mut StateStats,
) {
    match dst {
        Operand::ZeroPage(zp) => {
            writeln!(out, "    ld [${:04X}], a", NES_RAM_BASE + zp as u16).unwrap();
        }
        Operand::Absolute(addr) => {
            writeln!(out, "    ld [${:04X}], a", direct_ram_addr(addr).unwrap()).unwrap();
        }
        Operand::ZeroPageX(zp) => {
            writeln!(out, "    push af").unwrap();
            ensure_x(out, state, stats);
            writeln!(out, "    ld a, b ; superblock cached X index").unwrap();
            stats.x_reload_avoided += 1;
            stats.x_index_uses += 1;
            writeln!(out, "    add ${zp:02X}").unwrap();
            writeln!(out, "    ld l, a").unwrap();
            writeln!(out, "    ld h, $C0").unwrap();
            writeln!(out, "    pop af").unwrap();
            writeln!(out, "    ld [hl], a").unwrap();
        }
        Operand::ZeroPageY(zp) => {
            writeln!(out, "    push af").unwrap();
            ensure_y(out, state, stats);
            writeln!(out, "    ld a, c ; superblock cached Y index").unwrap();
            stats.y_reload_avoided += 1;
            stats.y_index_uses += 1;
            writeln!(out, "    add ${zp:02X}").unwrap();
            writeln!(out, "    ld l, a").unwrap();
            writeln!(out, "    ld h, $C0").unwrap();
            writeln!(out, "    pop af").unwrap();
            writeln!(out, "    ld [hl], a").unwrap();
        }
        Operand::AbsoluteX(addr) => {
            writeln!(out, "    push af").unwrap();
            ensure_x(out, state, stats);
            writeln!(out, "    ld hl, ${:04X}", NES_RAM_BASE + addr).unwrap();
            writeln!(out, "    ld a, b ; superblock cached X index").unwrap();
            stats.x_reload_avoided += 1;
            stats.x_index_uses += 1;
            emit_add_a_to_hl(out);
            writeln!(out, "    pop af").unwrap();
            writeln!(out, "    ld [hl], a").unwrap();
        }
        Operand::AbsoluteY(addr) => {
            writeln!(out, "    push af").unwrap();
            ensure_y(out, state, stats);
            writeln!(out, "    ld hl, ${:04X}", NES_RAM_BASE + addr).unwrap();
            writeln!(out, "    ld a, c ; superblock cached Y index").unwrap();
            stats.y_reload_avoided += 1;
            stats.y_index_uses += 1;
            emit_add_a_to_hl(out);
            writeln!(out, "    pop af").unwrap();
            writeln!(out, "    ld [hl], a").unwrap();
        }
        Operand::Immediate(_) | Operand::IndexedIndirect(_) | Operand::IndirectIndexed(_) => {
            unreachable!()
        }
    }
}

fn load_reg_to_a(
    out: &mut String,
    reg: Register,
    state: &mut TraceState,
    stats: &mut StateStats,
) -> bool {
    match reg {
        Register::A => ensure_a(out, state, stats),
        Register::X => {
            ensure_x(out, state, stats);
            writeln!(out, "    ld a, b ; superblock cached X").unwrap();
            stats.x_reload_avoided += 1;
        }
        Register::Y => {
            ensure_y(out, state, stats);
            writeln!(out, "    ld a, c ; superblock cached Y").unwrap();
            stats.y_reload_avoided += 1;
        }
        Register::Sp => return false,
    }
    true
}

fn write_reg_from_a(
    out: &mut String,
    reg: Register,
    state: &mut TraceState,
    stats: &mut StateStats,
) -> bool {
    match reg {
        Register::A => write_a_resident(state, stats),
        Register::X => {
            writeln!(out, "    ld b, a ; superblock X becomes resident").unwrap();
            state.x_b = true;
            state.x_dirty = true;
            stats.x_stores_deferred += 1;
        }
        Register::Y => {
            writeln!(out, "    ld c, a ; superblock Y becomes resident").unwrap();
            state.y_c = true;
            state.y_dirty = true;
            stats.y_stores_deferred += 1;
        }
        Register::Sp => return false,
    }
    true
}

fn emit_fast_modify_value(out: &mut String, modify: ModifyOp, stats: &mut StateStats) {
    match modify {
        ModifyOp::Inc => {
            writeln!(out, "    inc a").unwrap();
            emit_update_nz(out);
        }
        ModifyOp::Dec => {
            writeln!(out, "    dec a").unwrap();
            emit_update_nz(out);
        }
        ModifyOp::Asl | ModifyOp::Lsr | ModifyOp::Rol | ModifyOp::Ror => {
            if matches!(modify, ModifyOp::Rol | ModifyOp::Ror) {
                // nes_c_shadow is normalized to 0/1. RRA copies bit 0 into
                // host carry without branches; LD below preserves that carry.
                // B/C remain untouched because they belong to resident X/Y.
                writeln!(out, "    ld d, a ; superblock rotate input").unwrap();
                writeln!(out, "    ldh a, [nes_c_shadow]").unwrap();
                writeln!(out, "    rra ; seed host carry from 6502 C").unwrap();
                writeln!(out, "    ld a, d").unwrap();
            }

            let name = match modify {
                ModifyOp::Asl => {
                    // ADD A,A is the one-byte/one-M-cycle LR35902 form of ASL.
                    writeln!(out, "    add a ; superblock fast ASL").unwrap();
                    "ASL"
                }
                ModifyOp::Lsr => {
                    writeln!(out, "    srl a ; superblock fast LSR").unwrap();
                    "LSR"
                }
                ModifyOp::Rol => {
                    writeln!(out, "    rl a ; superblock fast ROL").unwrap();
                    "ROL"
                }
                ModifyOp::Ror => {
                    writeln!(out, "    rr a ; superblock fast ROR").unwrap();
                    "ROR"
                }
                _ => unreachable!(),
            };
            let _ = name;
            writeln!(out, "    ld e, a ; superblock shift/rotate result").unwrap();

            // Branchless carry capture: LD preserves carry and RL moves C into
            // bit 0, yielding the canonical normalized 0/1 shadow.
            writeln!(out, "    ld a, $00").unwrap();
            writeln!(out, "    rl a ; capture shift/rotate carry").unwrap();
            writeln!(out, "    ldh [nes_c_shadow], a").unwrap();

            writeln!(out, "    ld a, e").unwrap();
            emit_update_nz(out);
            stats.fast_shifts += 1;
        }
    }
}

fn emit_fast_modify_memory(
    out: &mut String,
    mem: Operand,
    modify: ModifyOp,
    state: &mut TraceState,
    stats: &mut StateStats,
) {
    match mem {
        Operand::ZeroPage(zp) => {
            let addr = NES_RAM_BASE + zp as u16;
            writeln!(out, "    ld a, [${addr:04X}]").unwrap();
            emit_fast_modify_value(out, modify, stats);
            writeln!(out, "    ld [${addr:04X}], a").unwrap();
        }
        Operand::Absolute(addr) => {
            let mapped = direct_ram_addr(addr).unwrap();
            writeln!(out, "    ld a, [${mapped:04X}]").unwrap();
            emit_fast_modify_value(out, modify, stats);
            writeln!(out, "    ld [${mapped:04X}], a").unwrap();
        }
        Operand::ZeroPageX(zp) => {
            ensure_x(out, state, stats);
            writeln!(out, "    ld a, b ; superblock cached X index").unwrap();
            stats.x_reload_avoided += 1;
            stats.x_index_uses += 1;
            writeln!(out, "    add ${zp:02X}").unwrap();
            writeln!(out, "    ld l, a").unwrap();
            writeln!(out, "    ld h, $C0").unwrap();
            writeln!(out, "    ld a, [hl]").unwrap();
            emit_fast_modify_value(out, modify, stats);
            writeln!(out, "    ld [hl], a ; reuse indexed RMW address").unwrap();
            stats.fast_rmw_addr_reuse += 1;
        }
        Operand::ZeroPageY(zp) => {
            ensure_y(out, state, stats);
            writeln!(out, "    ld a, c ; superblock cached Y index").unwrap();
            stats.y_reload_avoided += 1;
            stats.y_index_uses += 1;
            writeln!(out, "    add ${zp:02X}").unwrap();
            writeln!(out, "    ld l, a").unwrap();
            writeln!(out, "    ld h, $C0").unwrap();
            writeln!(out, "    ld a, [hl]").unwrap();
            emit_fast_modify_value(out, modify, stats);
            writeln!(out, "    ld [hl], a ; reuse indexed RMW address").unwrap();
            stats.fast_rmw_addr_reuse += 1;
        }
        Operand::AbsoluteX(addr) => {
            ensure_x(out, state, stats);
            writeln!(out, "    ld hl, ${:04X}", NES_RAM_BASE + addr).unwrap();
            writeln!(out, "    ld a, b ; superblock cached X index").unwrap();
            stats.x_reload_avoided += 1;
            stats.x_index_uses += 1;
            emit_add_a_to_hl(out);
            writeln!(out, "    ld a, [hl]").unwrap();
            emit_fast_modify_value(out, modify, stats);
            writeln!(out, "    ld [hl], a ; reuse indexed RMW address").unwrap();
            stats.fast_rmw_addr_reuse += 1;
        }
        Operand::AbsoluteY(addr) => {
            ensure_y(out, state, stats);
            writeln!(out, "    ld hl, ${:04X}", NES_RAM_BASE + addr).unwrap();
            writeln!(out, "    ld a, c ; superblock cached Y index").unwrap();
            stats.y_reload_avoided += 1;
            stats.y_index_uses += 1;
            emit_add_a_to_hl(out);
            writeln!(out, "    ld a, [hl]").unwrap();
            emit_fast_modify_value(out, modify, stats);
            writeln!(out, "    ld [hl], a ; reuse indexed RMW address").unwrap();
            stats.fast_rmw_addr_reuse += 1;
        }
        Operand::Immediate(_) | Operand::IndexedIndirect(_) | Operand::IndirectIndexed(_) => {
            unreachable!()
        }
    }
}

fn fast_op_supported(op: &IrOp) -> bool {
    match *op {
        IrOp::SetFlag { .. } | IrOp::Nop => true,
        IrOp::Load { dst, src } => dst != Register::Sp && fast_operand_supported(src),
        IrOp::Store { src, dst } => src != Register::Sp && fast_store_supported(dst),
        IrOp::Transfer { src, dst, .. } => src != Register::Sp && dst != Register::Sp,
        IrOp::Inc(reg) | IrOp::Dec(reg) => reg != Register::Sp,
        IrOp::Logic {
            rhs: Operand::Immediate(_),
            ..
        } => true,
        IrOp::Arithmetic { rhs, .. } => fast_operand_supported(rhs),
        IrOp::Compare { reg, rhs } => reg != Register::Sp && fast_operand_supported(rhs),
        IrOp::Modify {
            op:
                ModifyOp::Inc
                | ModifyOp::Dec
                | ModifyOp::Asl
                | ModifyOp::Lsr
                | ModifyOp::Rol
                | ModifyOp::Ror,
            target: ModifyTarget::Accumulator,
        } => true,
        IrOp::Modify {
            op:
                ModifyOp::Inc
                | ModifyOp::Dec
                | ModifyOp::Asl
                | ModifyOp::Lsr
                | ModifyOp::Rol
                | ModifyOp::Ror,
            target: ModifyTarget::Memory(mem),
        } => fast_operand_supported(mem) && fast_store_supported(mem),
        _ => false,
    }
}

fn emit_fast_op(out: &mut String, op: &IrOp, state: &mut TraceState, stats: &mut StateStats) {
    debug_assert!(fast_op_supported(op));

    match *op {
        IrOp::SetFlag { .. }
        | IrOp::Load {
            dst: Register::X | Register::Y,
            ..
        }
        | IrOp::Store {
            src: Register::X | Register::Y,
            ..
        }
        | IrOp::Transfer {
            src: Register::X | Register::Y,
            dst: Register::X | Register::Y,
            ..
        }
        | IrOp::Inc(Register::X | Register::Y)
        | IrOp::Dec(Register::X | Register::Y)
        | IrOp::Modify {
            target: ModifyTarget::Memory(_),
            ..
        }
        | IrOp::Compare { .. } => clobber_a(out, state, stats),
        IrOp::Load {
            dst: Register::A, ..
        }
        | IrOp::Transfer {
            dst: Register::A, ..
        } => discard_a(state),
        _ => {}
    }

    match *op {
        IrOp::SetFlag { flag, value } => match flag {
            Flag::Carry => {
                writeln!(out, "    ld a, ${:02X}", if value { 1 } else { 0 }).unwrap();
                writeln!(out, "    ldh [nes_c_shadow], a").unwrap();
            }
            Flag::Zero => {
                writeln!(out, "    ld a, ${:02X}", if value { 0 } else { 1 }).unwrap();
                writeln!(out, "    ldh [nes_z_shadow], a").unwrap();
            }
            Flag::Negative => {
                writeln!(out, "    ld a, ${:02X}", if value { 0x80 } else { 0 }).unwrap();
                writeln!(out, "    ldh [nes_n_shadow], a").unwrap();
            }
            _ => {
                writeln!(out, "    ldh a, [nes_p]").unwrap();
                if value {
                    writeln!(out, "    or ${:02X}", flag_mask(flag)).unwrap();
                } else {
                    writeln!(out, "    and ${:02X}", !flag_mask(flag)).unwrap();
                }
                writeln!(out, "    ldh [nes_p], a").unwrap();
            }
        },
        IrOp::Load { dst, src } => {
            emit_operand_load(out, src, state, stats);
            let _ = write_reg_from_a(out, dst, state, stats);
            emit_update_nz(out);
        }
        IrOp::Store { src, dst } => {
            let _ = load_reg_to_a(out, src, state, stats);
            emit_operand_store(out, dst, state, stats);
        }
        IrOp::Transfer {
            src,
            dst,
            update_nz,
        } => {
            let _ = load_reg_to_a(out, src, state, stats);
            let _ = write_reg_from_a(out, dst, state, stats);
            if update_nz {
                emit_update_nz(out);
            }
        }
        IrOp::Inc(reg) | IrOp::Dec(reg) => {
            let inc = matches!(*op, IrOp::Inc(_));
            match reg {
                Register::A => {
                    ensure_a(out, state, stats);
                    writeln!(out, "    {} a", if inc { "inc" } else { "dec" }).unwrap();
                    write_a_resident(state, stats);
                }
                Register::X => {
                    ensure_x(out, state, stats);
                    writeln!(out, "    {} b", if inc { "inc" } else { "dec" }).unwrap();
                    writeln!(out, "    ld a, b").unwrap();
                    state.x_dirty = true;
                    stats.x_stores_deferred += 1;
                }
                Register::Y => {
                    ensure_y(out, state, stats);
                    writeln!(out, "    {} c", if inc { "inc" } else { "dec" }).unwrap();
                    writeln!(out, "    ld a, c").unwrap();
                    state.y_dirty = true;
                    stats.y_stores_deferred += 1;
                }
                Register::Sp => unreachable!(),
            }
            emit_update_nz(out);
        }
        IrOp::Logic {
            op,
            rhs: Operand::Immediate(imm),
        } => {
            ensure_a(out, state, stats);
            match op {
                LogicOp::And => writeln!(out, "    and ${imm:02X}").unwrap(),
                LogicOp::Ora => writeln!(out, "    or ${imm:02X}").unwrap(),
                LogicOp::Eor => writeln!(out, "    xor ${imm:02X}").unwrap(),
            }
            write_a_resident(state, stats);
            emit_update_nz(out);
        }
        IrOp::Arithmetic { op, rhs } => {
            // Capture architectural A before operand/address work can use host A.
            // B/C remain reserved for resident X/Y; D holds the 6502 lhs.
            ensure_a(out, state, stats);
            writeln!(out, "    ld d, a ; superblock resident arithmetic lhs").unwrap();
            match rhs {
                Operand::Immediate(imm) => {
                    writeln!(out, "    ld e, ${imm:02X}").unwrap();
                }
                _ => {
                    emit_operand_load(out, rhs, state, stats);
                    writeln!(out, "    ld e, a ; superblock arithmetic RHS").unwrap();
                }
            }
            if op == ArithmeticOp::Sbc {
                // 6502 SBC is A + (~rhs) + C; using complemented E lets the
                // ADC overflow identity below match SBC exactly as well.
                writeln!(out, "    ld a, e").unwrap();
                writeln!(out, "    cpl").unwrap();
                writeln!(out, "    ld e, a").unwrap();
            }

            writeln!(out, "    ld a, d").unwrap();
            writeln!(out, "    ldh a, [nes_c_shadow]").unwrap();
            writeln!(out, "    and a").unwrap();
            writeln!(out, "    jr z, :+").unwrap();
            writeln!(out, "    scf").unwrap();
            writeln!(out, "    jr :++").unwrap();
            writeln!(out, ":").unwrap();
            writeln!(out, "    and a").unwrap();
            writeln!(out, ":").unwrap();
            writeln!(out, "    ld a, d").unwrap();
            writeln!(
                out,
                "    adc e ; superblock fast {}",
                if op == ArithmeticOp::Adc {
                    "ADC"
                } else {
                    "SBC"
                }
            )
            .unwrap();
            writeln!(out, "    ld l, a ; arithmetic result").unwrap();

            // Capture 6502 carry before any flag-clobbering status work.
            writeln!(out, "    ld a, $00").unwrap();
            writeln!(out, "    jr nc, :+").unwrap();
            writeln!(out, "    inc a").unwrap();
            writeln!(out, ":").unwrap();
            writeln!(out, "    ldh [nes_c_shadow], a").unwrap();

            // Only V remains material in nes_p. For SBC, E is ~rhs, so the
            // normal ADC identity ~(lhs^E)&(lhs^result) becomes the SBC
            // identity (lhs^rhs)&(lhs^result).
            writeln!(out, "    ldh a, [nes_p]").unwrap();
            writeln!(out, "    and $BF").unwrap();
            writeln!(out, "    ldh [nes_p], a").unwrap();
            writeln!(out, "    ld a, d").unwrap();
            writeln!(out, "    xor e").unwrap();
            writeln!(out, "    cpl").unwrap();
            writeln!(out, "    ld h, a").unwrap();
            writeln!(out, "    ld a, d").unwrap();
            writeln!(out, "    xor l").unwrap();
            writeln!(out, "    and h").unwrap();
            writeln!(out, "    and $80").unwrap();
            writeln!(out, "    jr z, :+").unwrap();
            writeln!(out, "    ldh a, [nes_p]").unwrap();
            writeln!(out, "    or $40").unwrap();
            writeln!(out, "    ldh [nes_p], a").unwrap();
            writeln!(out, ":").unwrap();

            writeln!(out, "    ld a, l").unwrap();
            write_a_resident(state, stats);
            emit_update_nz(out);
            stats.fast_arithmetic += 1;
        }
        IrOp::Modify { op: modify, target } => match target {
            ModifyTarget::Accumulator => {
                ensure_a(out, state, stats);
                emit_fast_modify_value(out, modify, stats);
                write_a_resident(state, stats);
            }
            ModifyTarget::Memory(mem) => {
                emit_fast_modify_memory(out, mem, modify, state, stats);
            }
        },
        IrOp::Compare { reg, rhs } => {
            match rhs {
                Operand::Immediate(imm) => {
                    writeln!(out, "    ld e, ${imm:02X}").unwrap();
                }
                _ => {
                    emit_operand_load(out, rhs, state, stats);
                    writeln!(out, "    ld e, a ; superblock compare RHS").unwrap();
                }
            }
            match reg {
                Register::A => writeln!(out, "    ldh a, [nes_a]").unwrap(),
                Register::X => {
                    ensure_x(out, state, stats);
                    writeln!(out, "    ld a, b ; superblock compare cached X").unwrap();
                    stats.x_reload_avoided += 1;
                }
                Register::Y => {
                    ensure_y(out, state, stats);
                    writeln!(out, "    ld a, c ; superblock compare cached Y").unwrap();
                    stats.y_reload_avoided += 1;
                }
                Register::Sp => unreachable!(),
            }
            // 6502 CMP/CPX/CPY: C is set when lhs >= rhs; Z/N follow lhs-rhs.
            // Inline it so resident B/C survive instead of forcing a barrier.
            writeln!(out, "    sub e ; superblock fast compare").unwrap();
            writeln!(out, "    ldh [nes_z_shadow], a").unwrap();
            writeln!(out, "    ldh [nes_n_shadow], a").unwrap();
            writeln!(out, "    ld a, $00").unwrap();
            writeln!(out, "    jr c, :+").unwrap();
            writeln!(out, "    inc a").unwrap();
            writeln!(out, ":").unwrap();
            writeln!(out, "    ldh [nes_c_shadow], a").unwrap();
            stats.fast_compares += 1;
        }
        IrOp::Nop => {}
        _ => unreachable!(),
    }
    stats.fast_ops += 1;
}

fn emit_barrier_ops(
    out: &mut String,
    ops: &[IrOp],
    state: &mut TraceState,
    stats: &mut StateStats,
) {
    if ops.is_empty() {
        return;
    }
    sync_state(out, state, stats);
    invalidate_state(state);
    out.push_str(&lr35902::emit_ops(ops));
    stats.barriers += 1;
}

pub fn emit_cfg_with_interrupts(
    graph: &ControlFlowGraph,
    options: EmitOptions,
    nmi: u16,
    irq: u16,
) -> String {
    let mut out = String::new();
    writeln!(out, "; Generated by nes2gbc stateful superblock emitter").unwrap();
    writeln!(out, "; A lives in host A, X in B, Y in C across safe trace edges; dirty canonical HRAM is materialized at barriers").unwrap();
    writeln!(out).unwrap();

    let limit = options.max_blocks.unwrap_or(graph.blocks.len());
    let selected = select_reachable(graph, options.reset, limit);
    let banks = assign_code_banks(graph, &selected);
    let poll_points = nmi_poll_points(graph, &selected);
    let nmi_exclusive = nmi_exclusive_blocks(graph, &selected, options.reset, nmi, irq);
    let plan = plan_superblocks(graph, &selected, &banks, &poll_points, &nmi_exclusive);

    if plan.disabled_for_unresolved_indirect {
        println!(
            "superblock: disabled because selected CFG contains unresolved indirect control flow"
        );
    } else {
        println!(
            "superblock: formed {} multi-block trace(s), chained {} same-bank edge(s) ({} NMI-private multi-entry/dead-poll), elided {} unconditional JMP(s)",
            plan.multi_block_traces,
            plan.chained_edges,
            plan.nmi_private_chained_edges,
            plan.elided_jumps
        );
    }
    println!(
        "nmi-superblock-proof: {} block(s) proven NMI-exclusive",
        nmi_exclusive.len()
    );

    writeln!(out, "SECTION \"Generated NES reset entry\", ROM0").unwrap();
    writeln!(out, "nes_reset:").unwrap();
    writeln!(out, "    ld a, [nes_reset_count]").unwrap();
    writeln!(out, "    inc a").unwrap();
    writeln!(out, "    ld [nes_reset_count], a").unwrap();
    emit_pc_dispatch(&mut out, options.reset);
    writeln!(out).unwrap();

    let selected_list = &plan.order;
    let mut pending_continuation: Option<(u16, u16)> = None;
    let mut section_offs: BTreeMap<u16, usize> = BTreeMap::new();
    let mut section_pc = 0usize;
    let mut state = TraceState::default();
    let mut stats = StateStats::default();
    let mut entry_contracts: BTreeMap<u16, (u16, TraceState)> = BTreeMap::new();

    for (idx, addr) in selected_list.iter().copied().enumerate() {
        let block = graph.blocks.get(&addr).expect("selected block must exist");
        let bank = banks[&addr];
        let continuing = matches!(
            pending_continuation,
            Some((target, from_bank)) if target == addr && from_bank == bank
        );

        if continuing {
            pending_continuation = None;
        } else {
            if let Some((target, from_bank)) = pending_continuation.take() {
                sync_state(&mut out, &mut state, &mut stats);
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
            state = TraceState::default();
            writeln!(out, "SECTION \"NES block {addr:04X}\", ROMX, BANK[{bank}]").unwrap();
            section_offs.clear();
            section_pc = 0;
        }

        if continuing {
            entry_contracts.insert(addr, (bank, state));
            writeln!(out, "nes_{addr:04X}_trace:").unwrap();
        } else {
            section_offs.insert(block.start, section_pc);
            writeln!(out, "nes_{:04X}:", block.start).unwrap();
        }

        writeln!(out, "IF DEF(NES2GBC_PROFILE_TRACE)").unwrap();
        if continuing && state.a_live {
            writeln!(
                out,
                "    push af ; preserve resident A across profile trace"
            )
            .unwrap();
        }
        writeln!(out, "    ld hl, ${:04X}", block.start).unwrap();
        writeln!(out, "    call nes_profile_trace_pc").unwrap();
        if continuing && state.a_live {
            writeln!(out, "    pop af").unwrap();
        }
        writeln!(out, "ENDC").unwrap();

        if options.debug_trace {
            let before = out.len();
            if continuing && state.a_live {
                writeln!(out, "    push af ; preserve resident A across debug trace").unwrap();
            }
            writeln!(out, "    ld a, ${:02X}", (block.start >> 8) as u8).unwrap();
            writeln!(out, "    ld [nes_debug_pc_hi], a").unwrap();
            writeln!(out, "    ld a, ${:02X}", block.start as u8).unwrap();
            writeln!(out, "    ld [nes_debug_pc_lo], a").unwrap();
            if continuing && state.a_live {
                writeln!(out, "    pop af").unwrap();
            }
            section_pc += approx_code_bytes(&out[before..]);
        }

        if poll_points.contains(&block.start) {
            let exclusive = nmi_exclusive.contains(&block.start);
            if continuing {
                // Inside a proven NMI-only trace, nested NES NMIs are impossible.
                // Crossing this safe point changes no architectural behavior.
                debug_assert!(exclusive);
                writeln!(out, "    ; NMI-private trace crosses dead safe-point poll").unwrap();
            } else {
                if exclusive {
                    writeln!(
                        out,
                        "    ; NMI-exclusive safe-point retained as analysis barrier"
                    )
                    .unwrap();
                    writeln!(out, "IF 0").unwrap();
                }
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
        }

        let mut pending: Vec<IrOp> = Vec::new();
        let write_insn_comment =
            |out: &mut String, instruction: &crate::cpu6502::DecodedInstruction| {
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

        for instruction in &block.instructions {
            match ir::lower_instruction(*instruction) {
                Ok(ops) => {
                    let in_trace_jump = matches!(
                        ops.as_slice(),
                        [IrOp::Jump(target)] if plan.next.get(&block.start).copied() == Some(*target)
                    );
                    if in_trace_jump {
                        if !pending.is_empty() {
                            let before = out.len();
                            emit_barrier_ops(&mut out, &pending, &mut state, &mut stats);
                            section_pc += approx_code_bytes(&out[before..]);
                            pending.clear();
                        }
                        write_insn_comment(&mut out, instruction);
                        writeln!(out, "    ; superblock: same-bank unique-entry JMP elided with resident X/Y state").unwrap();
                        continue;
                    }

                    let mut probe = String::new();
                    if emit_static_control(&mut probe, &ops, bank, &banks, None, 0) {
                        if !pending.is_empty() {
                            let before = out.len();
                            emit_barrier_ops(&mut out, &pending, &mut state, &mut stats);
                            section_pc += approx_code_bytes(&out[before..]);
                            pending.clear();
                        }
                        sync_state(&mut out, &mut state, &mut stats);
                        write_insn_comment(&mut out, instruction);
                        let before = out.len();
                        let _ = emit_static_control(
                            &mut out,
                            &ops,
                            bank,
                            &banks,
                            Some(&section_offs),
                            section_pc,
                        );
                        section_pc += approx_code_bytes(&out[before..]);
                        continue;
                    }

                    if ops.len() == 1 && fast_op_supported(&ops[0]) {
                        if !pending.is_empty() {
                            let before = out.len();
                            emit_barrier_ops(&mut out, &pending, &mut state, &mut stats);
                            section_pc += approx_code_bytes(&out[before..]);
                            pending.clear();
                        }
                        write_insn_comment(&mut out, instruction);
                        let before = out.len();
                        emit_fast_op(&mut out, &ops[0], &mut state, &mut stats);
                        section_pc += approx_code_bytes(&out[before..]);
                    } else {
                        write_insn_comment(&mut out, instruction);
                        pending.extend(ops);
                    }
                }
                Err(err) => {
                    if !pending.is_empty() {
                        let before = out.len();
                        emit_barrier_ops(&mut out, &pending, &mut state, &mut stats);
                        section_pc += approx_code_bytes(&out[before..]);
                        pending.clear();
                    }
                    sync_state(&mut out, &mut state, &mut stats);
                    invalidate_state(&mut state);
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
            emit_barrier_ops(&mut out, &pending, &mut state, &mut stats);
            section_pc += approx_code_bytes(&out[before..]);
        }

        if let Some(target) = plan.next.get(&block.start).copied() {
            debug_assert_eq!(selected_list.get(idx + 1).copied(), Some(target));
            pending_continuation = Some((target, bank));
        } else if let Some(last) = block.instructions.last() {
            if is_branch(last.def.mnemonic) || !terminal_mnemonic(last.def.mnemonic) {
                if let Some(target) = block
                    .edges
                    .iter()
                    .find(|edge| matches!(edge.kind, EdgeKind::Fallthrough))
                    .and_then(|edge| edge.target)
                {
                    sync_state(&mut out, &mut state, &mut stats);
                    let before = out.len();
                    emit_known_target(
                        &mut out,
                        target,
                        bank,
                        &banks,
                        Some(&section_offs),
                        section_pc,
                    );
                    section_pc += approx_code_bytes(&out[before..]);
                }
            }
        }
        writeln!(out).unwrap();
    }

    if let Some((target, from_bank)) = pending_continuation.take() {
        sync_state(&mut out, &mut state, &mut stats);
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

    for (addr, (bank, contract)) in &entry_contracts {
        writeln!(
            out,
            "SECTION \"NES canonical superblock entry {addr:04X}\", ROMX, BANK[{bank}]"
        )
        .unwrap();
        writeln!(out, "nes_{addr:04X}:").unwrap();
        if contract.x_b {
            writeln!(out, "    ldh a, [nes_x]").unwrap();
            writeln!(out, "    ld b, a ; canonical adapter X").unwrap();
        }
        if contract.y_c {
            writeln!(out, "    ldh a, [nes_y]").unwrap();
            writeln!(out, "    ld c, a ; canonical adapter Y").unwrap();
        }
        if contract.a_live {
            writeln!(out, "    ldh a, [nes_a] ; canonical adapter A").unwrap();
        }
        writeln!(out, "    jp nes_{addr:04X}_trace").unwrap();
        writeln!(out).unwrap();
        stats.canonical_adapters += 1;
    }

    println!(
        "superblock-state: A avoided {} reload(s), deferred {} store(s), materialized {}, seeds {}; avoided {} X + {} Y HRAM reload(s); deferred {} X + {} Y canonical store(s); materialized {} X + {} Y at barriers/side exits; cached indexed uses X={} Y={}; seeds X={} Y={}; fast ops {} ({} compares, {} ADC/SBC, {} shifts/rotates, {} indexed RMW addr reuses); barriers {}; canonical adapters {}",
        stats.a_reload_avoided,
        stats.a_stores_deferred,
        stats.a_materialized,
        stats.a_seed_loads,
        stats.x_reload_avoided,
        stats.y_reload_avoided,
        stats.x_stores_deferred,
        stats.y_stores_deferred,
        stats.x_materialized,
        stats.y_materialized,
        stats.x_index_uses,
        stats.y_index_uses,
        stats.x_seed_loads,
        stats.y_seed_loads,
        stats.fast_ops,
        stats.fast_compares,
        stats.fast_arithmetic,
        stats.fast_shifts,
        stats.fast_rmw_addr_reuse,
        stats.barriers,
        stats.canonical_adapters,
    );

    emit_dispatch_tables(&mut out, &selected);
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cfg;

    #[test]
    fn unique_entry_chain_gets_private_trace_entry_and_canonical_adapter() {
        let mut prg = vec![0xEA; 0x8000];
        // LDX #$04 / JMP $8010 ; target STX $00 / RTS.
        prg[0..5].copy_from_slice(&[0xA2, 0x04, 0x4C, 0x10, 0x80]);
        prg[0x10..0x13].copy_from_slice(&[0x86, 0x00, 0x60]);
        let graph = cfg::discover(0, &prg, &[0x8000]).unwrap();
        let asm = emit_cfg_with_interrupts(
            &graph,
            EmitOptions {
                reset: 0x8000,
                max_blocks: Some(8),
                debug_trace: false,
            },
            0x8000,
            0x8000,
        );
        assert!(asm.contains("nes_8010_trace:"));
        assert!(asm.contains("nes_8010:"));
        assert!(asm.contains("canonical adapter X"));
        assert!(asm.contains("superblock X becomes resident"));
        assert!(asm.contains("superblock cached X"));
    }

    #[test]
    fn multiply_reached_target_is_not_internalized() {
        let mut prg = vec![0xEA; 0x8000];
        prg[0..8].copy_from_slice(&[0xD0, 0x03, 0x4C, 0x05, 0x80, 0xA9, 0x01, 0x60]);
        let graph = cfg::discover(0, &prg, &[0x8000]).unwrap();
        let selected: BTreeSet<u16> = graph.blocks.keys().copied().collect();
        let banks = assign_code_banks(&graph, &selected);
        let polls = nmi_poll_points(&graph, &selected);
        let plan = plan_superblocks(&graph, &selected, &banks, &polls, &BTreeSet::new());
        assert!(!plan.next.values().any(|&target| target == 0x8005));
    }

    #[test]
    fn compare_keeps_dirty_x_resident_until_real_control_barrier() {
        let mut prg = vec![0xEA; 0x8000];
        // LDX #$04 / CPX #$03 / BNE $8008 / NOP / RTS / NOP / RTS.
        prg[0..9].copy_from_slice(&[0xA2, 0x04, 0xE0, 0x03, 0xD0, 0x02, 0xEA, 0x60, 0x60]);
        let graph = cfg::discover(0, &prg, &[0x8000]).unwrap();
        let asm = emit_cfg_with_interrupts(
            &graph,
            EmitOptions {
                reset: 0x8000,
                max_blocks: Some(8),
                debug_trace: false,
            },
            0x8000,
            0x8000,
        );
        let compare = asm.find("; $8002: $E0 Cpx Immediate").unwrap();
        let branch = asm.find("; $8004: $D0 Bne Relative").unwrap();
        let between = &asm[compare..branch];
        assert!(between.contains("superblock compare cached X"));
        assert!(between.contains("superblock fast compare"));
    }

    #[test]
    fn arithmetic_keeps_dirty_x_resident_and_emits_adc_sbc_inline() {
        let mut prg = vec![0xEA; 0x8000];
        // LDX #4 / LDA #$7F / CLC / ADC #1 / SEC / SBC #1 / STX $00 / RTS.
        prg[0..13].copy_from_slice(&[
            0xA2, 0x04, 0xA9, 0x7F, 0x18, 0x69, 0x01, 0x38, 0xE9, 0x01, 0x86, 0x00, 0x60,
        ]);
        let graph = cfg::discover(0, &prg, &[0x8000]).unwrap();
        let asm = emit_cfg_with_interrupts(
            &graph,
            EmitOptions {
                reset: 0x8000,
                max_blocks: Some(8),
                debug_trace: false,
            },
            0x8000,
            0x8000,
        );
        let adc = asm.find("; $8005: $69 Adc Immediate").unwrap();
        let store = asm.find("; $800A: $86 Stx ZeroPage").unwrap();
        let between = &asm[adc..store];
        assert!(between.contains("superblock fast ADC"));
        assert!(between.contains("superblock fast SBC"));
        assert!(!between.contains("superblock materialize X"));
        assert!(asm[store..].contains("superblock cached X"));
    }
    #[test]
    fn shifts_keep_dirty_x_resident_and_emit_inline() {
        let mut prg = vec![0xEA; 0x8000];
        // LDX #4 / LDA #$81 / ASL A / LSR A / SEC / ROL A / ROR A /
        // ASL $00,X / LSR $00,X / ROL $00,X / ROR $00,X / STX $10 / RTS.
        prg[0..20].copy_from_slice(&[
            0xA2, 0x04, 0xA9, 0x81, 0x0A, 0x4A, 0x38, 0x2A, 0x6A, 0x16, 0x00, 0x56, 0x00, 0x36,
            0x00, 0x76, 0x00, 0x86, 0x10, 0x60,
        ]);
        let graph = cfg::discover(0, &prg, &[0x8000]).unwrap();
        let asm = emit_cfg_with_interrupts(
            &graph,
            EmitOptions {
                reset: 0x8000,
                max_blocks: Some(8),
                debug_trace: false,
            },
            0x8000,
            0x8000,
        );
        let first_shift = asm.find("; $8004: $0A Asl Accumulator").unwrap();
        let store_x = asm.find("; $8011: $86 Stx ZeroPage").unwrap();
        let between = &asm[first_shift..store_x];
        assert!(between.contains("superblock fast ASL"));
        assert!(between.contains("superblock fast LSR"));
        assert!(between.contains("superblock fast ROL"));
        assert!(between.contains("superblock fast ROR"));
        assert!(between.contains("add a ; superblock fast ASL"));
        assert!(between.contains("rra ; seed host carry from 6502 C"));
        assert!(between.contains("rl a ; capture shift/rotate carry"));
        assert_eq!(between.matches("superblock cached X index").count(), 4);
        assert_eq!(between.matches("reuse indexed RMW address").count(), 4);
        assert!(!between.contains("push af"));
        assert!(!between.contains("superblock materialize X"));
        assert!(asm[store_x..].contains("superblock cached X"));
    }
    #[test]
    fn a_stays_dirty_across_store_logic_and_private_trace_edge() {
        let mut prg = vec![0xEA; 0x8000];
        // LDA #$3F / STA $00 / AND #$0F / JMP $8010 ; target STA $01 / RTS.
        prg[0..9].copy_from_slice(&[0xA9, 0x3F, 0x85, 0x00, 0x29, 0x0F, 0x4C, 0x10, 0x80]);
        prg[0x10..0x13].copy_from_slice(&[0x85, 0x01, 0x60]);
        let graph = cfg::discover(0, &prg, &[0x8000]).unwrap();
        let asm = emit_cfg_with_interrupts(
            &graph,
            EmitOptions {
                reset: 0x8000,
                max_blocks: Some(8),
                debug_trace: false,
            },
            0x8000,
            0x8000,
        );
        let lda = asm.find("; $8000: $A9 Lda Immediate").unwrap();
        let target = asm.find("nes_8010_trace:").unwrap();
        let rts = asm.find("; $8012: $60 Rts Implied").unwrap();
        let hot = &asm[lda..rts];
        assert!(hot.contains("superblock: same-bank unique-entry JMP elided"));
        assert!(!hot.contains("superblock materialize A"));
        assert!(!hot.contains("ldh a, [nes_a]"));
        assert!(asm[target..rts].contains("ld [$C001], a"));
        let after_rts = &asm[rts..];
        assert!(after_rts.contains("superblock materialize A"));
        assert!(asm.contains("ldh a, [nes_a] ; canonical adapter A"));
    }
}
