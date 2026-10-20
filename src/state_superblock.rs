use std::collections::{BTreeMap, BTreeSet};
use std::fmt::Write;

use crate::mir::MirBuf;

use crate::emit_common::*;
use crate::polls::nmi_poll_points;

use crate::{
    cfg::{ControlFlowGraph, EdgeKind},
    ir::{self, ArithmeticOp, Flag, IrOp, LogicOp, ModifyOp, ModifyTarget, Operand, Register},
    lr35902,
    recompile::EmitOptions,
};

const NES_RAM_BASE: u16 = 0xC000;

/// Compiler register-allocation level, from the NES2GBC_REGALLOC environment
/// variable (Makefile `REGALLOC=`). 0 keeps the previous emission exactly.
pub(crate) fn regalloc_level() -> u32 {
    use std::sync::OnceLock;
    static LEVEL: OnceLock<u32> = OnceLock::new();
    *LEVEL.get_or_init(|| {
        std::env::var("NES2GBC_REGALLOC")
            .ok()
            .and_then(|v| v.trim().parse().ok())
            .unwrap_or(0)
    })
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

pub(crate) fn preferred_successor(block: &crate::cfg::BasicBlock) -> Option<(u16, bool)> {
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


/// REGALLOC>=2: 6502 overflow (V) liveness over the whole CFG. Returns the PCs
/// of instructions after which V is provably dead on every path. V is read by
/// BVC/BVS, PHP and BRK, and by every executed NMI poll (the NMI entry
/// materializes P). RTS/RTI/BRK, unresolved or unselected edges and anything
/// unrecognized are treated as reads.
fn overflow_dead_after(
    graph: &ControlFlowGraph,
    selected: &BTreeSet<u16>,
    poll_points: &BTreeSet<u16>,
    nmi_exclusive: &BTreeSet<u16>,
) -> BTreeSet<u16> {
    use crate::cpu6502::Mnemonic::*;
    flag_dead_after(
        graph,
        selected,
        poll_points,
        nmi_exclusive,
        |m| matches!(m, Bvc | Bvs | Php | Brk),
        |m| matches!(m, Adc | Sbc | Bit | Clv | Plp | Rti),
    )
}

/// REGALLOC>=3: 6502 carry liveness (same conservative model as V).
fn carry_dead_after(
    graph: &ControlFlowGraph,
    selected: &BTreeSet<u16>,
    poll_points: &BTreeSet<u16>,
    nmi_exclusive: &BTreeSet<u16>,
) -> BTreeSet<u16> {
    use crate::cpu6502::Mnemonic::*;
    flag_dead_after(
        graph,
        selected,
        poll_points,
        nmi_exclusive,
        |m| matches!(m, Adc | Sbc | Rol | Ror | Bcc | Bcs | Php | Brk),
        |m| {
            matches!(
                m,
                Adc | Sbc | Cmp | Cpx | Cpy | Asl | Lsr | Rol | Ror | Clc | Sec | Plp | Rti
            )
        },
    )
}

/// Backward liveness of one 6502 status flag over the selected CFG.
fn flag_dead_after(
    graph: &ControlFlowGraph,
    selected: &BTreeSet<u16>,
    poll_points: &BTreeSet<u16>,
    nmi_exclusive: &BTreeSet<u16>,
    reads: impl Fn(crate::cpu6502::Mnemonic) -> bool,
    writes: impl Fn(crate::cpu6502::Mnemonic) -> bool,
) -> BTreeSet<u16> {
    use crate::cpu6502::Mnemonic::*;
    let polled = |addr: u16| poll_points.contains(&addr) && !nmi_exclusive.contains(&addr);

    let live_out_of = |addr: u16, live_in: &BTreeMap<u16, bool>| -> bool {
        let Some(block) = graph.blocks.get(&addr) else { return true };
        let Some(last) = block.instructions.last() else { return true };
        if matches!(last.def.mnemonic, Rts | Rti | Brk) || block.edges.is_empty() {
            return true;
        }
        block.edges.iter().any(|edge| match edge.target {
            Some(t) if selected.contains(&t) => live_in.get(&t).copied().unwrap_or(false),
            _ => true,
        })
    };
    let block_live_in = |addr: u16, out: bool| -> bool {
        let block = &graph.blocks[&addr];
        let mut live = out;
        for insn in block.instructions.iter().rev() {
            let m = insn.def.mnemonic;
            if writes(m) {
                live = false;
            }
            if reads(m) {
                live = true;
            }
        }
        live || polled(addr)
    };

    let mut live_in: BTreeMap<u16, bool> = selected.iter().map(|&a| (a, false)).collect();
    loop {
        let mut changed = false;
        for &addr in selected.iter().rev() {
            if !graph.blocks.contains_key(&addr) {
                continue;
            }
            let out = live_out_of(addr, &live_in);
            let new_in = block_live_in(addr, out);
            if new_in && !live_in[&addr] {
                live_in.insert(addr, true);
                changed = true;
            }
        }
        if !changed {
            break;
        }
    }

    let mut dead = BTreeSet::new();
    for &addr in selected {
        let Some(block) = graph.blocks.get(&addr) else { continue };
        let mut live = live_out_of(addr, &live_in);
        for insn in block.instructions.iter().rev() {
            if !live {
                dead.insert(insn.pc);
            }
            let m = insn.def.mnemonic;
            if writes(m) {
                live = false;
            }
            if reads(m) {
                live = true;
            }
        }
    }
    dead
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
struct TraceState {
    a_live: bool,
    a_dirty: bool,
    x_b: bool,
    x_dirty: bool,
    y_c: bool,
    y_dirty: bool,
    /// REGALLOC: 6502 carry statically known within the current block
    /// (after CLC/SEC). Never part of a trace entry contract.
    carry: Option<bool>,
    /// REGALLOC>=2: V is dead after the instruction being emitted.
    v_dead: bool,
    /// REGALLOC>=3: C is dead after the instruction being emitted.
    c_dead: bool,
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
    x_kept_across_barrier: usize,
    y_kept_across_barrier: usize,
}

fn direct_ram_addr(addr: u16) -> Option<u16> {
    if addr < 0x2000 {
        Some(NES_RAM_BASE + (addr & 0x07FF))
    } else {
        None
    }
}

fn emit_update_nz(out: &mut MirBuf) {
    out.ins("ldh", &["[nes_z_shadow]", "a"], None);
    out.ins("ldh", &["[nes_n_shadow]", "a"], None);
}

fn ensure_a(out: &mut MirBuf, state: &mut TraceState, stats: &mut StateStats) {
    if state.a_live {
        stats.a_reload_avoided += 1;
        return;
    }
    out.ins("ldh", &["a", "[nes_a]"], Some("superblock A cache seed"));
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

fn materialize_a(out: &mut MirBuf, state: &mut TraceState, stats: &mut StateStats) {
    if !state.a_dirty {
        return;
    }
    debug_assert!(state.a_live);
    out.ins("ldh", &["[nes_a]", "a"], Some("superblock materialize A"));
    state.a_dirty = false;
    stats.a_materialized += 1;
}

fn clobber_a(out: &mut MirBuf, state: &mut TraceState, stats: &mut StateStats) {
    materialize_a(out, state, stats);
    state.a_live = false;
}

fn ensure_x(out: &mut MirBuf, state: &mut TraceState, stats: &mut StateStats) {
    if state.x_b {
        return;
    }
    out.ins("ldh", &["a", "[nes_x]"], None);
    out.ins("ld", &["b", "a"], Some("superblock X cache seed"));
    state.x_b = true;
    state.x_dirty = false;
    stats.x_seed_loads += 1;
}

fn ensure_y(out: &mut MirBuf, state: &mut TraceState, stats: &mut StateStats) {
    if state.y_c {
        return;
    }
    out.ins("ldh", &["a", "[nes_y]"], None);
    out.ins("ld", &["c", "a"], Some("superblock Y cache seed"));
    state.y_c = true;
    state.y_dirty = false;
    stats.y_seed_loads += 1;
}

fn sync_xy(out: &mut MirBuf, state: &mut TraceState, stats: &mut StateStats) {
    if state.x_dirty {
        debug_assert!(state.x_b);
        out.ins("ld", &["a", "b"], Some("superblock materialize X"));
        out.ins("ldh", &["[nes_x]", "a"], None);
        state.x_dirty = false;
        stats.x_materialized += 1;
    }
    if state.y_dirty {
        debug_assert!(state.y_c);
        out.ins("ld", &["a", "c"], Some("superblock materialize Y"));
        out.ins("ldh", &["[nes_y]", "a"], None);
        state.y_dirty = false;
        stats.y_materialized += 1;
    }
}

fn sync_state(out: &mut MirBuf, state: &mut TraceState, stats: &mut StateStats) {
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

fn emit_add_a_to_hl(out: &mut MirBuf) {
    out.ins("add", &["l"], None);
    out.ins("ld", &["l", "a"], None);
    out.ins("jr", &["nc", ":+"], None);
    out.ins("inc", &["h"], None);
    out.label("");
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

/// HL = ($zp),Y with Y from host C (ensured resident). Clobbers A, DE, HL.
fn emit_indirect_y_addr(out: &mut MirBuf, zp: u8, state: &mut TraceState, stats: &mut StateStats) {
    let cached = state.y_c;
    out.ins("ld", &["e", format!("${zp:02X}").as_str()], None);
    out.ins("ld", &["d", "$C0"], None);
    out.ins("ld", &["a", "[de]"], None);
    out.ins("ld", &["l", "a"], None);
    out.ins("inc", &["e"], None);
    out.ins("ld", &["a", "[de]"], None);
    out.ins("ld", &["h", "a"], None);
    if cached {
        out.ins("ld", &["a", "c"], Some("superblock cached Y index"));
        stats.y_reload_avoided += 1;
        stats.y_index_uses += 1;
    } else {
        out.ins("ldh", &["a", "[nes_y]"], None);
    }
    emit_add_a_to_hl(out);
}

/// LDA ($zp),Y keeping X/Y resident in B/C: RAM inline, everything else
/// through nes_cpu_read_hi with BC saved around the call.
fn emit_indirect_y_load(out: &mut MirBuf, zp: u8, state: &mut TraceState, stats: &mut StateStats) {
    emit_indirect_y_addr(out, zp, state, stats);
    out.ins("ld", &["a", "h"], None);
    out.ins("cp", &["$20"], None);
    out.ins("jr", &["nc", ":+"], None);
    out.ins("and", &["$07"], None);
    out.ins("or", &["$C0"], None);
    out.ins("ld", &["h", "a"], None);
    out.ins("ld", &["a", "[hl]"], None);
    out.ins("jr", &[":++"], None);
    out.label("");
    let keep = state.x_b || state.y_c;
    if keep {
        out.ins("push", &["bc"], Some("superblock (zp),Y: keep cached X/Y"));
    }
    out.ins("call", &["nes_cpu_read_hi"], Some("inline RAM test failed: H >= $20"));
    if keep {
        out.ins("pop", &["bc"], None);
    }
    out.label("");
}

/// STA ($zp),Y with the value in host A; A is clobbered afterwards.
fn emit_indirect_y_store(out: &mut MirBuf, zp: u8, state: &mut TraceState, stats: &mut StateStats) {
    out.ins("push", &["af"], None);
    emit_indirect_y_addr(out, zp, state, stats);
    out.ins("ld", &["a", "h"], None);
    out.ins("cp", &["$20"], None);
    out.ins("jr", &["nc", ":+"], None);
    out.ins("and", &["$07"], None);
    out.ins("or", &["$C0"], None);
    out.ins("ld", &["h", "a"], None);
    out.ins("pop", &["af"], None);
    out.ins("ld", &["[hl]", "a"], None);
    out.ins("jr", &[":++"], None);
    out.label("");
    out.ins("pop", &["af"], None);
    out.ins("push", &["af"], Some("superblock (zp),Y store: A stays resident"));
    let keep = state.x_b || state.y_c;
    if keep {
        out.ins("push", &["bc"], Some("superblock (zp),Y: keep cached X/Y"));
    }
    out.ins("call", &["nes_cpu_write"], None);
    if keep {
        out.ins("pop", &["bc"], None);
    }
    out.ins("pop", &["af"], None);
    out.label("");
}

fn emit_operand_load(
    out: &mut MirBuf,
    src: Operand,
    state: &mut TraceState,
    stats: &mut StateStats,
) {
    match src {
        Operand::Immediate(v) => out.ins("ld", &["a", format!("${v:02X}").as_str()], None),
        Operand::IndirectIndexed(zp) => emit_indirect_y_load(out, zp, state, stats),
        Operand::ZeroPage(zp) => {
            out.ins("ld", &["a", format!("[${:04X}]", NES_RAM_BASE + zp as u16).as_str()], None);
        }
        Operand::Absolute(addr) => {
            out.ins("ld", &["a", format!("[${:04X}]", direct_ram_addr(addr).unwrap()).as_str()], None);
        }
        Operand::ZeroPageX(zp) => {
            ensure_x(out, state, stats);
            out.ins("ld", &["a", "b"], Some("superblock cached X index"));
            stats.x_reload_avoided += 1;
            stats.x_index_uses += 1;
            out.ins("add", &[format!("${zp:02X}").as_str()], None);
            out.ins("ld", &["l", "a"], None);
            out.ins("ld", &["h", "$C0"], None);
            out.ins("ld", &["a", "[hl]"], None);
        }
        Operand::ZeroPageY(zp) => {
            ensure_y(out, state, stats);
            out.ins("ld", &["a", "c"], Some("superblock cached Y index"));
            stats.y_reload_avoided += 1;
            stats.y_index_uses += 1;
            out.ins("add", &[format!("${zp:02X}").as_str()], None);
            out.ins("ld", &["l", "a"], None);
            out.ins("ld", &["h", "$C0"], None);
            out.ins("ld", &["a", "[hl]"], None);
        }
        Operand::AbsoluteX(addr) => {
            ensure_x(out, state, stats);
            out.ins("ld", &["hl", format!("${:04X}", NES_RAM_BASE + addr).as_str()], None);
            out.ins("ld", &["a", "b"], Some("superblock cached X index"));
            stats.x_reload_avoided += 1;
            stats.x_index_uses += 1;
            emit_add_a_to_hl(out);
            out.ins("ld", &["a", "[hl]"], None);
        }
        Operand::AbsoluteY(addr) => {
            ensure_y(out, state, stats);
            out.ins("ld", &["hl", format!("${:04X}", NES_RAM_BASE + addr).as_str()], None);
            out.ins("ld", &["a", "c"], Some("superblock cached Y index"));
            stats.y_reload_avoided += 1;
            stats.y_index_uses += 1;
            emit_add_a_to_hl(out);
            out.ins("ld", &["a", "[hl]"], None);
        }
        Operand::IndexedIndirect(_) => unreachable!(),
    }
}

fn emit_operand_store(
    out: &mut MirBuf,
    dst: Operand,
    state: &mut TraceState,
    stats: &mut StateStats,
) {
    match dst {
        Operand::ZeroPage(zp) => {
            out.ins("ld", &[format!("[${:04X}]", NES_RAM_BASE + zp as u16).as_str(), "a"], None);
        }
        Operand::Absolute(addr) => {
            out.ins("ld", &[format!("[${:04X}]", direct_ram_addr(addr).unwrap()).as_str(), "a"], None);
        }
        Operand::ZeroPageX(zp) => {
            out.ins("push", &["af"], None);
            ensure_x(out, state, stats);
            out.ins("ld", &["a", "b"], Some("superblock cached X index"));
            stats.x_reload_avoided += 1;
            stats.x_index_uses += 1;
            out.ins("add", &[format!("${zp:02X}").as_str()], None);
            out.ins("ld", &["l", "a"], None);
            out.ins("ld", &["h", "$C0"], None);
            out.ins("pop", &["af"], None);
            out.ins("ld", &["[hl]", "a"], None);
        }
        Operand::ZeroPageY(zp) => {
            out.ins("push", &["af"], None);
            ensure_y(out, state, stats);
            out.ins("ld", &["a", "c"], Some("superblock cached Y index"));
            stats.y_reload_avoided += 1;
            stats.y_index_uses += 1;
            out.ins("add", &[format!("${zp:02X}").as_str()], None);
            out.ins("ld", &["l", "a"], None);
            out.ins("ld", &["h", "$C0"], None);
            out.ins("pop", &["af"], None);
            out.ins("ld", &["[hl]", "a"], None);
        }
        Operand::AbsoluteX(addr) => {
            out.ins("push", &["af"], None);
            ensure_x(out, state, stats);
            out.ins("ld", &["hl", format!("${:04X}", NES_RAM_BASE + addr).as_str()], None);
            out.ins("ld", &["a", "b"], Some("superblock cached X index"));
            stats.x_reload_avoided += 1;
            stats.x_index_uses += 1;
            emit_add_a_to_hl(out);
            out.ins("pop", &["af"], None);
            out.ins("ld", &["[hl]", "a"], None);
        }
        Operand::AbsoluteY(addr) => {
            out.ins("push", &["af"], None);
            ensure_y(out, state, stats);
            out.ins("ld", &["hl", format!("${:04X}", NES_RAM_BASE + addr).as_str()], None);
            out.ins("ld", &["a", "c"], Some("superblock cached Y index"));
            stats.y_reload_avoided += 1;
            stats.y_index_uses += 1;
            emit_add_a_to_hl(out);
            out.ins("pop", &["af"], None);
            out.ins("ld", &["[hl]", "a"], None);
        }
        Operand::Immediate(_) | Operand::IndexedIndirect(_) | Operand::IndirectIndexed(_) => {
            unreachable!()
        }
    }
}

fn load_reg_to_a(
    out: &mut MirBuf,
    reg: Register,
    state: &mut TraceState,
    stats: &mut StateStats,
) -> bool {
    match reg {
        Register::A => ensure_a(out, state, stats),
        Register::X => {
            ensure_x(out, state, stats);
            out.ins("ld", &["a", "b"], Some("superblock cached X"));
            stats.x_reload_avoided += 1;
        }
        Register::Y => {
            ensure_y(out, state, stats);
            out.ins("ld", &["a", "c"], Some("superblock cached Y"));
            stats.y_reload_avoided += 1;
        }
        Register::Sp => return false,
    }
    true
}

fn write_reg_from_a(
    out: &mut MirBuf,
    reg: Register,
    state: &mut TraceState,
    stats: &mut StateStats,
) -> bool {
    match reg {
        Register::A => write_a_resident(state, stats),
        Register::X => {
            out.ins("ld", &["b", "a"], Some("superblock X becomes resident"));
            state.x_b = true;
            state.x_dirty = true;
            stats.x_stores_deferred += 1;
        }
        Register::Y => {
            out.ins("ld", &["c", "a"], Some("superblock Y becomes resident"));
            state.y_c = true;
            state.y_dirty = true;
            stats.y_stores_deferred += 1;
        }
        Register::Sp => return false,
    }
    true
}

fn emit_fast_modify_value(
    out: &mut MirBuf,
    modify: ModifyOp,
    known_carry: Option<bool>,
    c_dead: bool,
    stats: &mut StateStats,
) {
    match modify {
        ModifyOp::Inc => {
            out.ins("inc", &["a"], None);
            emit_update_nz(out);
        }
        ModifyOp::Dec => {
            out.ins("dec", &["a"], None);
            emit_update_nz(out);
        }
        ModifyOp::Asl | ModifyOp::Lsr | ModifyOp::Rol | ModifyOp::Ror => {
            if matches!(modify, ModifyOp::Rol | ModifyOp::Ror) {
                // nes_c_shadow is normalized to 0/1. RRA copies bit 0 into
                // host carry without branches; LD below preserves that carry.
                // B/C remain untouched because they belong to resident X/Y.
                match known_carry {
                    Some(false) => {
                        out.ins("and", &["a"], Some("known 6502 C=0"));
                    }
                    Some(true) => {
                        out.ins("scf", &[], Some("known 6502 C=1"));
                    }
                    None => {
                        out.ins("ld", &["d", "a"], Some("superblock rotate input"));
                        out.ins("ldh", &["a", "[nes_c_shadow]"], None);
                        out.ins("rra", &[], Some("seed host carry from 6502 C"));
                        out.ins("ld", &["a", "d"], None);
                    }
                }
            }

            let name = match modify {
                ModifyOp::Asl => {
                    // ADD A,A is the one-byte/one-M-cycle LR35902 form of ASL.
                    out.ins("add", &["a"], Some("superblock fast ASL"));
                    "ASL"
                }
                ModifyOp::Lsr => {
                    out.ins("srl", &["a"], Some("superblock fast LSR"));
                    "LSR"
                }
                ModifyOp::Rol => {
                    out.ins("rl", &["a"], Some("superblock fast ROL"));
                    "ROL"
                }
                ModifyOp::Ror => {
                    out.ins("rr", &["a"], Some("superblock fast ROR"));
                    "ROR"
                }
                _ => unreachable!(),
            };
            let _ = name;
            if c_dead {
                writeln!(out, "    ; C dead on every path (compiler carry liveness)").unwrap();
            } else {
                out.ins("ld", &["e", "a"], Some("superblock shift/rotate result"));

                // Branchless carry capture: LD preserves carry and RL moves C into
                // bit 0, yielding the canonical normalized 0/1 shadow.
                out.ins("ld", &["a", "$00"], None);
                out.ins("rl", &["a"], Some("capture shift/rotate carry"));
                out.ins("ldh", &["[nes_c_shadow]", "a"], None);

                out.ins("ld", &["a", "e"], None);
            }
            emit_update_nz(out);
            stats.fast_shifts += 1;
        }
    }
}

fn rmw_hl_enabled() -> bool {
    std::env::var("NES2GBC_RMW_HL").map(|v| v != "0").unwrap_or(true)
}

/// Native read-modify-write on [hl] (HL already holds the GB address):
/// INC/DEC [hl], SLA/SRL/RL/RR [hl]. Host carry is seeded from the 6502 C
/// only for ROL/ROR, captured only when C is live, and the result is
/// reloaded into A only for the Z/N shadows (dead stores are removed by the
/// global dead-HRAM passes). A is clobbered exactly as in the load/op/store
/// form. NES2GBC_RMW_HL=0 restores the old form.
fn emit_fast_modify_hl(
    out: &mut MirBuf,
    modify: ModifyOp,
    known_carry: Option<bool>,
    c_dead: bool,
    stats: &mut StateStats,
) {
    match modify {
        ModifyOp::Inc => out.ins("inc", &["[hl]"], Some("native RMW INC")),
        ModifyOp::Dec => out.ins("dec", &["[hl]"], Some("native RMW DEC")),
        ModifyOp::Asl | ModifyOp::Lsr | ModifyOp::Rol | ModifyOp::Ror => {
            if matches!(modify, ModifyOp::Rol | ModifyOp::Ror) {
                match known_carry {
                    Some(false) => out.ins("and", &["a"], Some("known 6502 C=0")),
                    Some(true) => out.ins("scf", &[], Some("known 6502 C=1")),
                    None => {
                        out.ins("ldh", &["a", "[nes_c_shadow]"], None);
                        out.ins("rra", &[], Some("seed host carry from 6502 C"));
                    }
                }
            }
            let op = match modify {
                ModifyOp::Asl => "sla",
                ModifyOp::Lsr => "srl",
                ModifyOp::Rol => "rl",
                _ => "rr",
            };
            writeln!(out, "    {op} [hl] ; native RMW shift/rotate").unwrap();
            if c_dead {
                writeln!(out, "    ; C dead on every path (compiler carry liveness)").unwrap();
            } else {
                out.ins("ld", &["a", "$00"], None);
                out.ins("rl", &["a"], Some("capture shift/rotate carry"));
                out.ins("ldh", &["[nes_c_shadow]", "a"], None);
            }
            stats.fast_shifts += 1;
        }
    }
    out.ins("ld", &["a", "[hl]"], Some("RMW result for Z/N"));
    emit_update_nz(out);
}

fn emit_fast_modify_memory(
    out: &mut MirBuf,
    mem: Operand,
    modify: ModifyOp,
    state: &mut TraceState,
    stats: &mut StateStats,
) {
    match mem {
        Operand::ZeroPage(zp) => {
            let addr = NES_RAM_BASE + zp as u16;
            if rmw_hl_enabled() {
                out.ins("ld", &["hl", format!("${addr:04X}").as_str()], None);
                emit_fast_modify_hl(out, modify, state.carry, state.c_dead, stats);
                return;
            }
            out.ins("ld", &["a", format!("[${addr:04X}]").as_str()], None);
            emit_fast_modify_value(out, modify, state.carry, state.c_dead, stats);
            out.ins("ld", &[format!("[${addr:04X}]").as_str(), "a"], None);
        }
        Operand::Absolute(addr) => {
            let mapped = direct_ram_addr(addr).unwrap();
            if rmw_hl_enabled() {
                out.ins("ld", &["hl", format!("${mapped:04X}").as_str()], None);
                emit_fast_modify_hl(out, modify, state.carry, state.c_dead, stats);
                return;
            }
            out.ins("ld", &["a", format!("[${mapped:04X}]").as_str()], None);
            emit_fast_modify_value(out, modify, state.carry, state.c_dead, stats);
            out.ins("ld", &[format!("[${mapped:04X}]").as_str(), "a"], None);
        }
        Operand::ZeroPageX(zp) => {
            ensure_x(out, state, stats);
            out.ins("ld", &["a", "b"], Some("superblock cached X index"));
            stats.x_reload_avoided += 1;
            stats.x_index_uses += 1;
            out.ins("add", &[format!("${zp:02X}").as_str()], None);
            out.ins("ld", &["l", "a"], None);
            out.ins("ld", &["h", "$C0"], None);
            if rmw_hl_enabled() {
                emit_fast_modify_hl(out, modify, state.carry, state.c_dead, stats);
            } else {
                out.ins("ld", &["a", "[hl]"], None);
                emit_fast_modify_value(out, modify, state.carry, state.c_dead, stats);
                out.ins("ld", &["[hl]", "a"], Some("reuse indexed RMW address"));
            }
            stats.fast_rmw_addr_reuse += 1;
        }
        Operand::ZeroPageY(zp) => {
            ensure_y(out, state, stats);
            out.ins("ld", &["a", "c"], Some("superblock cached Y index"));
            stats.y_reload_avoided += 1;
            stats.y_index_uses += 1;
            out.ins("add", &[format!("${zp:02X}").as_str()], None);
            out.ins("ld", &["l", "a"], None);
            out.ins("ld", &["h", "$C0"], None);
            if rmw_hl_enabled() {
                emit_fast_modify_hl(out, modify, state.carry, state.c_dead, stats);
            } else {
                out.ins("ld", &["a", "[hl]"], None);
                emit_fast_modify_value(out, modify, state.carry, state.c_dead, stats);
                out.ins("ld", &["[hl]", "a"], Some("reuse indexed RMW address"));
            }
            stats.fast_rmw_addr_reuse += 1;
        }
        Operand::AbsoluteX(addr) => {
            ensure_x(out, state, stats);
            out.ins("ld", &["hl", format!("${:04X}", NES_RAM_BASE + addr).as_str()], None);
            out.ins("ld", &["a", "b"], Some("superblock cached X index"));
            stats.x_reload_avoided += 1;
            stats.x_index_uses += 1;
            emit_add_a_to_hl(out);
            if rmw_hl_enabled() {
                emit_fast_modify_hl(out, modify, state.carry, state.c_dead, stats);
            } else {
                out.ins("ld", &["a", "[hl]"], None);
                emit_fast_modify_value(out, modify, state.carry, state.c_dead, stats);
                out.ins("ld", &["[hl]", "a"], Some("reuse indexed RMW address"));
            }
            stats.fast_rmw_addr_reuse += 1;
        }
        Operand::AbsoluteY(addr) => {
            ensure_y(out, state, stats);
            out.ins("ld", &["hl", format!("${:04X}", NES_RAM_BASE + addr).as_str()], None);
            out.ins("ld", &["a", "c"], Some("superblock cached Y index"));
            stats.y_reload_avoided += 1;
            stats.y_index_uses += 1;
            emit_add_a_to_hl(out);
            if rmw_hl_enabled() {
                emit_fast_modify_hl(out, modify, state.carry, state.c_dead, stats);
            } else {
                out.ins("ld", &["a", "[hl]"], None);
                emit_fast_modify_value(out, modify, state.carry, state.c_dead, stats);
                out.ins("ld", &["[hl]", "a"], Some("reuse indexed RMW address"));
            }
            stats.fast_rmw_addr_reuse += 1;
        }
        Operand::Immediate(_) | Operand::IndexedIndirect(_) | Operand::IndirectIndexed(_) => {
            unreachable!()
        }
    }
}

fn park_enabled() -> bool {
    std::env::var("NES2GBC_STATE_PARK_A").map(|v| v != "0").unwrap_or(true)
}

fn indy_alu_enabled() -> bool {
    indy_enabled() && std::env::var("NES2GBC_STATE_INDY_ALU").map(|v| v != "0").unwrap_or(true)
}

fn indy_enabled() -> bool {
    std::env::var("NES2GBC_STATE_INDY").map(|v| v != "0").unwrap_or(true)
}

fn fast_op_supported(op: &IrOp) -> bool {
    match *op {
        IrOp::SetFlag { .. } | IrOp::Nop => true,
        IrOp::Load { dst: Register::A, src: Operand::IndirectIndexed(_) }
        | IrOp::Store { src: Register::A, dst: Operand::IndirectIndexed(_) }
        => indy_enabled(),
        IrOp::Logic { rhs: Operand::IndirectIndexed(_), .. }
        | IrOp::Arithmetic { rhs: Operand::IndirectIndexed(_), .. } => indy_alu_enabled(),
        IrOp::Compare { reg, rhs: Operand::IndirectIndexed(_) } => {
            reg != Register::Sp && indy_alu_enabled()
        }
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

fn emit_fast_op(out: &mut MirBuf, op: &IrOp, state: &mut TraceState, stats: &mut StateStats) {
    debug_assert!(fast_op_supported(op));

    let park = state.a_live && state.a_dirty
        && park_enabled()
        && matches!(*op, IrOp::Inc(Register::X | Register::Y) | IrOp::Dec(Register::X | Register::Y));
    if park {
        out.ins("ld", &["d", "a"], Some("superblock park A across INX/INY/DEX/DEY"));
    }

    match *op {
        _ if park => {}
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
                out.ins("ld", &["a", format!("${:02X}", if value { 1 } else { 0 }).as_str()], None);
                out.ins("ldh", &["[nes_c_shadow]", "a"], None);
                if regalloc_level() >= 1 {
                    state.carry = Some(value);
                }
            }
            Flag::Zero => {
                out.ins("ld", &["a", format!("${:02X}", if value { 0 } else { 1 }).as_str()], None);
                out.ins("ldh", &["[nes_z_shadow]", "a"], None);
            }
            Flag::Negative => {
                out.ins("ld", &["a", format!("${:02X}", if value { 0x80 } else { 0 }).as_str()], None);
                out.ins("ldh", &["[nes_n_shadow]", "a"], None);
            }
            _ => {
                out.ins("ldh", &["a", "[nes_p]"], None);
                if value {
                    out.ins("or", &[format!("${:02X}", flag_mask(flag)).as_str()], None);
                } else {
                    out.ins("and", &[format!("${:02X}", !flag_mask(flag)).as_str()], None);
                }
                out.ins("ldh", &["[nes_p]", "a"], None);
            }
        },
        IrOp::Load { dst: Register::A, src: Operand::IndirectIndexed(zp) } => {
            emit_indirect_y_load(out, zp, state, stats);
            let _ = write_reg_from_a(out, Register::A, state, stats);
            emit_update_nz(out);
        }
        IrOp::Store { src: Register::A, dst: Operand::IndirectIndexed(zp) } => {
            let _ = load_reg_to_a(out, Register::A, state, stats);
            emit_indirect_y_store(out, zp, state, stats);
        }
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
                    out.ins("ld", &["a", "b"], None);
                    state.x_dirty = true;
                    stats.x_stores_deferred += 1;
                }
                Register::Y => {
                    ensure_y(out, state, stats);
                    writeln!(out, "    {} c", if inc { "inc" } else { "dec" }).unwrap();
                    out.ins("ld", &["a", "c"], None);
                    state.y_dirty = true;
                    stats.y_stores_deferred += 1;
                }
                Register::Sp => unreachable!(),
            }
            emit_update_nz(out);
            if park {
                out.ins("ld", &["a", "d"], Some("superblock unpark A"));
            }
        }
        IrOp::Logic {
            op,
            rhs: Operand::IndirectIndexed(zp),
        } => {
            ensure_a(out, state, stats);
            out.ins("push", &["af"], Some("superblock (zp),Y logic lhs"));
            emit_indirect_y_load(out, zp, state, stats);
            out.ins("ld", &["e", "a"], None);
            out.ins("pop", &["af"], None);
            match op {
                LogicOp::And => out.ins("and", &["e"], None),
                LogicOp::Ora => out.ins("or", &["e"], None),
                LogicOp::Eor => out.ins("xor", &["e"], None),
            }
            write_a_resident(state, stats);
            emit_update_nz(out);
        }
        IrOp::Logic {
            op,
            rhs: Operand::Immediate(imm),
        } => {
            ensure_a(out, state, stats);
            match op {
                LogicOp::And => out.ins("and", &[format!("${imm:02X}").as_str()], None),
                LogicOp::Ora => out.ins("or", &[format!("${imm:02X}").as_str()], None),
                LogicOp::Eor => out.ins("xor", &[format!("${imm:02X}").as_str()], None),
            }
            write_a_resident(state, stats);
            emit_update_nz(out);
        }
        IrOp::Arithmetic { op, rhs } => {
            // Capture architectural A before operand/address work can use host A.
            // B/C remain reserved for resident X/Y; D holds the 6502 lhs.
            ensure_a(out, state, stats);
            if let Operand::IndirectIndexed(zp) = rhs {
                out.ins("push", &["af"], Some("superblock (zp),Y arithmetic lhs"));
                emit_indirect_y_load(out, zp, state, stats);
                out.ins("ld", &["e", "a"], Some("superblock arithmetic RHS"));
                out.ins("pop", &["af"], None);
            }
            out.ins("ld", &["d", "a"], Some("superblock resident arithmetic lhs"));
            match rhs {
                Operand::IndirectIndexed(_) => {}
                Operand::Immediate(imm) => {
                    out.ins("ld", &["e", format!("${imm:02X}").as_str()], None);
                }
                _ => {
                    emit_operand_load(out, rhs, state, stats);
                    out.ins("ld", &["e", "a"], Some("superblock arithmetic RHS"));
                }
            }
            if op == ArithmeticOp::Sbc {
                // 6502 SBC is A + (~rhs) + C; using complemented E lets the
                // ADC overflow identity below match SBC exactly as well.
                out.ins("ld", &["a", "e"], None);
                out.ins("cpl", &[], None);
                out.ins("ld", &["e", "a"], None);
            }

            match state.carry {
                Some(false) => {
                    out.ins("and", &["a"], Some("known 6502 C=0 (CLC)"));
                }
                Some(true) => {
                    out.ins("scf", &[], Some("known 6502 C=1 (SEC)"));
                }
                None => {
                    // nes_c_shadow is normalized to 0/1: RRA seeds host C.
                    out.ins("ldh", &["a", "[nes_c_shadow]"], None);
                    out.ins("rra", &[], Some("seed host carry from 6502 C"));
                }
            }
            out.ins("ld", &["a", "d"], None);
            out.ins(
                "adc",
                &["e"],
                Some(if op == ArithmeticOp::Adc { "superblock fast ADC" } else { "superblock fast SBC" }),
            );
            out.ins("ld", &["l", "a"], Some("arithmetic result"));

            // Capture 6502 carry before any flag-clobbering status work.
            if state.c_dead {
                writeln!(out, "    ; C dead on every path (compiler carry liveness)").unwrap();
            } else if regalloc_level() >= 2 {
                out.ins("ld", &["a", "$00"], None);
                out.ins("rla", &[], Some("branchless carry capture"));
            } else {
                out.ins("ld", &["a", "$00"], None);
                out.ins("jr", &["nc", ":+"], None);
                out.ins("inc", &["a"], None);
                out.label("");
            }
            if !state.c_dead {
                out.ins("ldh", &["[nes_c_shadow]", "a"], None);
            }
            if state.v_dead {
                writeln!(out, "    ; V dead on every path (compiler overflow liveness)").unwrap();
                out.ins("ld", &["a", "l"], None);
                write_a_resident(state, stats);
                emit_update_nz(out);
                stats.fast_arithmetic += 1;
                state.carry = None;
                stats.fast_ops += 1;
                return;
            }

            // Only V remains material in nes_p. For SBC, E is ~rhs, so the
            // normal ADC identity ~(lhs^E)&(lhs^result) becomes the SBC
            // identity (lhs^rhs)&(lhs^result).
            out.ins("ldh", &["a", "[nes_p]"], None);
            out.ins("and", &["$BF"], None);
            out.ins("ldh", &["[nes_p]", "a"], None);
            out.ins("ld", &["a", "d"], None);
            out.ins("xor", &["e"], None);
            out.ins("cpl", &[], None);
            out.ins("ld", &["h", "a"], None);
            out.ins("ld", &["a", "d"], None);
            out.ins("xor", &["l"], None);
            out.ins("and", &["h"], None);
            out.ins("and", &["$80"], None);
            out.ins("jr", &["z", ":+"], None);
            out.ins("ldh", &["a", "[nes_p]"], None);
            out.ins("or", &["$40"], None);
            out.ins("ldh", &["[nes_p]", "a"], None);
            out.label("");

            out.ins("ld", &["a", "l"], None);
            write_a_resident(state, stats);
            emit_update_nz(out);
            stats.fast_arithmetic += 1;
        }
        IrOp::Modify { op: modify, target } => match target {
            ModifyTarget::Accumulator => {
                ensure_a(out, state, stats);
                emit_fast_modify_value(out, modify, state.carry, state.c_dead, stats);
                write_a_resident(state, stats);
            }
            ModifyTarget::Memory(mem) => {
                emit_fast_modify_memory(out, mem, modify, state, stats);
            }
        },
        IrOp::Compare { reg, rhs } => {
            match rhs {
                Operand::Immediate(imm) => {
                    out.ins("ld", &["e", format!("${imm:02X}").as_str()], None);
                }
                _ => {
                    emit_operand_load(out, rhs, state, stats);
                    out.ins("ld", &["e", "a"], Some("superblock compare RHS"));
                }
            }
            match reg {
                Register::A => out.ins("ldh", &["a", "[nes_a]"], None),
                Register::X => {
                    ensure_x(out, state, stats);
                    out.ins("ld", &["a", "b"], Some("superblock compare cached X"));
                    stats.x_reload_avoided += 1;
                }
                Register::Y => {
                    ensure_y(out, state, stats);
                    out.ins("ld", &["a", "c"], Some("superblock compare cached Y"));
                    stats.y_reload_avoided += 1;
                }
                Register::Sp => unreachable!(),
            }
            // 6502 CMP/CPX/CPY: C is set when lhs >= rhs; Z/N follow lhs-rhs.
            // Inline it so resident B/C survive instead of forcing a barrier.
            out.ins("sub", &["e"], Some("superblock fast compare"));
            out.ins("ldh", &["[nes_z_shadow]", "a"], None);
            out.ins("ldh", &["[nes_n_shadow]", "a"], None);
            if state.c_dead {
                writeln!(out, "    ; C dead on every path (compiler carry liveness)").unwrap();
            } else {
                out.ins("ld", &["a", "$00"], None);
                out.ins("jr", &["c", ":+"], None);
                out.ins("inc", &["a"], None);
                out.label("");
                out.ins("ldh", &["[nes_c_shadow]", "a"], None);
            }
            stats.fast_compares += 1;
        }
        IrOp::Nop => {}
        _ => unreachable!(),
    }
    if matches!(
        *op,
        IrOp::Arithmetic { .. }
            | IrOp::Compare { .. }
            | IrOp::Modify {
                op: ModifyOp::Asl | ModifyOp::Lsr | ModifyOp::Rol | ModifyOp::Ror,
                ..
            }
    ) {
        state.carry = None;
    }
    stats.fast_ops += 1;
}

/// Stage-2 X/Y carrying knobs. `NES2GBC_XY_CARRY=0` disables stage 2 and
/// reproduces stage-1 output byte-identically; `x` / `y` carry one register.
/// `NES2GBC_XY_KEEP=0` keeps the cross-block contracts but drops the
/// helper-clobber-table residency across barriers.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub(crate) struct CarryCfg {
    pub x: bool,
    pub y: bool,
    pub keep: bool,
    /// NES2GBC_XY_DIRTY=1: allow contracts whose register may be newer than
    /// HRAM. Off by default: the text post-passes (native loop/leaf
    /// replacements) assume HRAM is authoritative at every 6502 control
    /// transfer, which clean contracts preserve.
    pub dirty: bool,
}

fn carry_keep() -> (bool, bool) {
    static K: std::sync::OnceLock<(bool, bool)> = std::sync::OnceLock::new();
    *K.get_or_init(|| {
        let c = carry_cfg();
        (c.keep && c.x, c.keep && c.y)
    })
}

pub(crate) fn carry_cfg() -> CarryCfg {
    let v = std::env::var("NES2GBC_XY_CARRY").unwrap_or_default();
    let (x, y) = match v.as_str() {
        "0" => (false, false),
        "x" => (true, false),
        "y" => (false, true),
        "xy" => (true, true),
        // Default: X only. Carrying Y measured as a net loss (SMB +3% wcost:
        // predecessors must load C for the contract more often than it saves).
        _ => (true, false),
    };
    let keep = (x || y) && std::env::var("NES2GBC_XY_KEEP").as_deref() != Ok("0");
    let dirty = (x || y) && std::env::var("NES2GBC_XY_DIRTY").as_deref() == Ok("1");
    CarryCfg { x, y, keep, dirty }
}

/// Register contract of a block's `nes_XXXX_rc` entry: for X (in B) and Y
/// (in C), None = canonical HRAM only, Some(false) = resident and in sync
/// with HRAM, Some(true) = resident and possibly newer than HRAM (the block
/// publishes it at its next barrier/exit).
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
struct Contract {
    x: Option<bool>,
    y: Option<bool>,
}

impl Contract {
    fn is_empty(&self) -> bool {
        self.x.is_none() && self.y.is_none()
    }
}

/// Predecessor arrivals observed at a contract entry during one pass.
#[derive(Debug, Clone, Copy, Default)]
struct Arrivals {
    x_res: u32,
    x_dirty: u32,
    y_res: u32,
    y_dirty: u32,
}

fn refine_contracts(cur: &BTreeMap<u16, Contract>, arr: &BTreeMap<u16, Arrivals>) -> BTreeMap<u16, Contract> {
    // Monotone: a slot only moves Dirty -> Clean -> None.
    let lower = |c: Option<bool>, res: u32, dirty: u32| -> Option<bool> {
        let obs = if dirty > 0 { Some(true) } else if res > 0 { Some(false) } else { None };
        match (c, obs) {
            (None, _) | (_, None) => None,
            (Some(a), Some(b)) => Some(a && b),
        }
    };
    let mut out = BTreeMap::new();
    for (&t, &c) in cur {
        let a = arr.get(&t).copied().unwrap_or_default();
        let n = Contract { x: lower(c.x, a.x_res, a.x_dirty), y: lower(c.y, a.y_res, a.y_dirty) };
        if !n.is_empty() {
            out.insert(t, n);
        }
    }
    out
}

fn materialize_x(out: &mut MirBuf, state: &mut TraceState, stats: &mut StateStats) {
    if state.x_dirty {
        out.ins("ld", &["a", "b"], Some("superblock materialize X"));
        out.ins("ldh", &["[nes_x]", "a"], None);
        state.x_dirty = false;
        stats.x_materialized += 1;
    }
}

fn materialize_y(out: &mut MirBuf, state: &mut TraceState, stats: &mut StateStats) {
    if state.y_dirty {
        out.ins("ld", &["a", "c"], Some("superblock materialize Y"));
        out.ins("ldh", &["[nes_y]", "a"], None);
        state.y_dirty = false;
        stats.y_materialized += 1;
    }
}

/// Exit towards `target` through its register-contract entry when the
/// target has one in this bank and at least one contract register is
/// resident: publish A and every non-contract register, reconcile the
/// contract registers (load the missing one, publish a dirty one the
/// contract wants clean) and return the label to jump to. None leaves the
/// state untouched (caller does the canonical sync + jump).
#[allow(clippy::too_many_arguments)]
fn carry_exit(
    out: &mut MirBuf,
    state: &mut TraceState,
    stats: &mut StateStats,
    target: u16,
    bank: u16,
    banks: &BTreeMap<u16, u16>,
    contracts: &BTreeMap<u16, Contract>,
    arrivals: &mut BTreeMap<u16, Arrivals>,
) -> Option<String> {
    let c = contracts.get(&target).copied().filter(|c| !c.is_empty())?;
    if banks.get(&target).copied() != Some(bank) {
        return None;
    }
    let x_hit = c.x.is_some() && state.x_b;
    let y_hit = c.y.is_some() && state.y_c;
    if !x_hit && !y_hit {
        return None;
    }
    let a = arrivals.entry(target).or_default();
    if x_hit {
        a.x_res += 1;
        a.x_dirty += state.x_dirty as u32;
    }
    if y_hit {
        a.y_res += 1;
        a.y_dirty += state.y_dirty as u32;
    }
    materialize_a(out, state, stats);
    if c.x != Some(true) {
        materialize_x(out, state, stats);
    }
    if c.y != Some(true) {
        materialize_y(out, state, stats);
    }
    if c.x.is_some() {
        ensure_x(out, state, stats);
    }
    if c.y.is_some() {
        ensure_y(out, state, stats);
    }
    state.a_live = false;
    Some(format!("nes_{target:04X}_trace"))
}

/// Same-section jump to a private label; `jr` when the (backward) target
/// offset is known and in range.
fn emit_local_jump(out: &mut MirBuf, label: &str, target_off: Option<usize>, section_pc: usize) {
    if let Some(off) = target_off {
        let delta = off as isize - (section_pc + 2) as isize;
        if (-128..128).contains(&delta) {
            out.ins("jr", &[label], None);
            return;
        }
    }
    out.ins("jp", &[label], None);
}

fn emit_barrier_ops(
    out: &mut MirBuf,
    ops: &[IrOp],
    state: &mut TraceState,
    stats: &mut StateStats,
) {
    if ops.is_empty() {
        return;
    }
    sync_state(out, state, stats);
    // A lone conditional branch lowers to a shadow-flag test (A) and a taken
    // exit through HL; B/C are untouched, so clean X/Y residency survives
    // into a chained fallthrough successor (canonical HRAM is already synced
    // for the taken edge and for the successor's canonical adapter).
    let branch_only = ops.len() == 1 && matches!(ops[0], IrOp::Branch { .. });
    let buf = lr35902::emit_ops_buf(ops);
    if branch_only {
        state.a_live = false;
    } else {
        // Stage 2: clean X/Y residency survives a barrier whose lowered MIR
        // (helper calls via the runtime clobber tables) leaves B/C and the
        // nes_x/nes_y slots untouched.
        let (want_x, want_y) = carry_keep();
        let (kb, kc) = if want_x || want_y { crate::clobbers::preserves_xy(&buf) } else { (false, false) };
        let (kb, kc) = (kb && want_x, kc && want_y);
        let (xb, yc) = (state.x_b && kb, state.y_c && kc);
        invalidate_state(state);
        state.x_b = xb;
        state.y_c = yc;
        stats.x_kept_across_barrier += xb as usize;
        stats.y_kept_across_barrier += yc as usize;
    }
    state.carry = None;
    out.append(buf);
    stats.barriers += 1;
}

/// Emit one basic block's instructions with block-local A/X/Y residency
/// (A in host A, X in B, Y in C). Nothing is resident on entry and every
/// dirty register is published before control ops, barriers and the block
/// end, so callers keep canonical HRAM state at every block boundary.
/// Returns false when an instruction faulted (the fault tail was emitted).
pub fn emit_block_body_local(
    out: &mut MirBuf,
    instructions: &[crate::cpu6502::DecodedInstruction],
) -> bool {
    emit_block_body_local_dead(out, instructions, &BTreeSet::new(), &BTreeSet::new())
}

/// As emit_block_body_local, with REGALLOC flag-liveness facts: `v_dead` /
/// `c_dead` hold the PCs of instructions after which V / C are dead on every
/// path (computed by the caller over its own CFG model).
pub fn emit_block_body_local_dead(
    out: &mut MirBuf,
    instructions: &[crate::cpu6502::DecodedInstruction],
    v_dead: &BTreeSet<u16>,
    c_dead: &BTreeSet<u16>,
) -> bool {
    let mut trace = LocalTrace::default();
    emit_block_body_chained(out, instructions, v_dead, c_dead, &mut trace, None)
}

/// Register residency carried between textually chained banked blocks.
#[derive(Debug, Default)]
pub struct LocalTrace {
    state: TraceState,
    stats: StateStats,
}

impl LocalTrace {
    /// Start a chained successor: block-local carry facts are dropped and the
    /// canonical-entry adapter that establishes the current residency from
    /// HRAM is returned (loads only, ends before the jump).
    pub fn enter_chained(&mut self) -> String {
        self.state.carry = None;
        let mut a = MirBuf::new();
        if self.state.x_b {
            a.ins("ldh", &["a", "[nes_x]"], None);
            a.ins("ld", &["b", "a"], Some("canonical adapter X"));
        }
        if self.state.y_c {
            a.ins("ldh", &["a", "[nes_y]"], None);
            a.ins("ld", &["c", "a"], Some("canonical adapter Y"));
        }
        if self.state.a_live {
            a.ins("ldh", &["a", "[nes_a]"], Some("canonical adapter A"));
        }
        a.into_string()
    }
    pub fn a_live(&self) -> bool {
        self.state.a_live
    }
}

/// Body emission with caller-owned residency. `chain_to = Some(pc)` means the
/// block continues textually into the translation of `pc`: dirty registers
/// stay resident (not published) and a final `JMP pc` is elided. Otherwise
/// all state is published at the end exactly like emit_block_body_local.
pub fn emit_block_body_chained(
    out: &mut MirBuf,
    instructions: &[crate::cpu6502::DecodedInstruction],
    v_dead: &BTreeSet<u16>,
    c_dead: &BTreeSet<u16>,
    trace: &mut LocalTrace,
    chain_to: Option<u16>,
) -> bool {
    emit_block_body_core(out, instructions, v_dead, c_dead, &mut trace.state, &mut trace.stats, chain_to)
}

fn emit_block_body_core(
    out: &mut MirBuf,
    instructions: &[crate::cpu6502::DecodedInstruction],
    v_dead: &BTreeSet<u16>,
    c_dead: &BTreeSet<u16>,
    state_ref: &mut TraceState,
    stats_ref: &mut StateStats,
    chain_to: Option<u16>,
) -> bool {
    let mut state = *state_ref;
    let mut stats = std::mem::take(stats_ref);
    let mut pending: Vec<IrOp> = Vec::new();
    for (i, instruction) in instructions.iter().enumerate() {
        if let Some(t) = chain_to {
            if i + 1 == instructions.len()
                && instruction.def.mnemonic == crate::cpu6502::Mnemonic::Jmp
                && instruction.def.mode == crate::cpu6502::AddressingMode::Absolute
                && instruction.operand == t
            {
                writeln!(out, "    ; ${:04X}: JMP ${t:04X} elided (banked trace)", instruction.pc).unwrap();
                break;
            }
        }
        let comment = format!(
            "    ; ${:04X}: ${:02X} {:?} {:?}\n",
            instruction.pc, instruction.opcode, instruction.def.mnemonic, instruction.def.mode
        );
        match ir::lower_instruction(*instruction) {
            Ok(ops) => {
                if ops.len() == 1 && fast_op_supported(&ops[0]) {
                    if !pending.is_empty() {
                        emit_barrier_ops(out, &pending, &mut state, &mut stats);
                        pending.clear();
                    }
                    out.push_str(&comment);
                    state.v_dead = v_dead.contains(&instruction.pc);
                    state.c_dead = c_dead.contains(&instruction.pc);
                    emit_fast_op(out, &ops[0], &mut state, &mut stats);
                    state.v_dead = false;
                    state.c_dead = false;
                } else {
                    out.push_str(&comment);
                    pending.extend(ops);
                }
            }
            Err(err) => {
                if !pending.is_empty() {
                    emit_barrier_ops(out, &pending, &mut state, &mut stats);
                    pending.clear();
                }
                sync_state(out, &mut state, &mut stats);
                out.push_str(&comment);
                writeln!(out, "    ; TODO {err}").unwrap();
                out.ins("ld", &["a", format!("${:02X}", instruction.pc as u8).as_str()], None);
                out.ins("ldh", &["[nes_fault_pc_lo]", "a"], None);
                out.ins("ld", &["a", format!("${:02X}", (instruction.pc >> 8) as u8).as_str()], None);
                out.ins("ldh", &["[nes_fault_pc_hi]", "a"], None);
                out.ins("jp", &["nes_unimplemented"], None);
                *state_ref = state;
                *stats_ref = stats;
                return false;
            }
        }
    }
    if !pending.is_empty() {
        emit_barrier_ops(out, &pending, &mut state, &mut stats);
    }
    if chain_to.is_none() {
        sync_state(out, &mut state, &mut stats);
    } else if std::env::var("NES2GBC_TRACE_MODE").as_deref() == Ok("sync") {
        sync_state(out, &mut state, &mut stats);
    } else if std::env::var("NES2GBC_TRACE_MODE").as_deref() == Ok("flush") {
        sync_state(out, &mut state, &mut stats);
        invalidate_state(&mut state);
    }
    *state_ref = state;
    *stats_ref = stats;
    true
}

pub fn emit_cfg_with_interrupts(
    graph: &ControlFlowGraph,
    options: EmitOptions,
    nmi: u16,
    irq: u16,
) -> String {
    let mut out = MirBuf::new();
    writeln!(out, "; Generated by nes2gbc stateful superblock emitter").unwrap();
    writeln!(out, "; A lives in host A, X in B, Y in C across safe trace edges; dirty canonical HRAM is materialized at barriers").unwrap();
    writeln!(out).unwrap();

    let limit = options.max_blocks.unwrap_or(graph.blocks.len());
    let selected = select_reachable(graph, options.reset, limit);
    let banks = assign_code_banks(graph, &selected, 254);
    let poll_points = nmi_poll_points(graph, &selected);
    let nmi_exclusive = nmi_exclusive_blocks(graph, &selected, options.reset, nmi, irq);
    let plan = plan_superblocks(graph, &selected, &banks, &poll_points, &nmi_exclusive);
    let v_dead = if regalloc_level() >= 2 {
        overflow_dead_after(graph, &selected, &poll_points, &nmi_exclusive)
    } else {
        BTreeSet::new()
    };
    let c_dead = if regalloc_level() >= 3 {
        carry_dead_after(graph, &selected, &poll_points, &nmi_exclusive)
    } else {
        BTreeSet::new()
    };

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
    if regalloc_level() >= 2 {
        println!(
            "regalloc: V dead after {} instruction(s), C dead after {}",
            v_dead.len(),
            c_dead.len()
        );
    }

    // Stage 2 (docs/IR_REWRITE.md): X/Y register contracts across static
    // block edges. Contracts are refined by re-emitting until the observed
    // predecessor arrivals stop changing (monotone; every pass is correct on
    // its own, the iteration only drops useless/dirty-free contracts).
    let carry = if options.debug_trace { CarryCfg::default() } else { carry_cfg() };
    let mut contracts: BTreeMap<u16, Contract> = BTreeMap::new();
    if carry.x || carry.y {
        for &addr in &plan.order {
            let continuing_target = plan.next.values().any(|&t| t == addr);
            let polled = poll_points.contains(&addr) && !nmi_exclusive.contains(&addr);
            if continuing_target || polled {
                continue;
            }
            contracts.insert(addr, Contract { x: carry.x.then_some(carry.dirty), y: carry.y.then_some(carry.dirty) });
        }
        // Debug bisection aid: NES2GBC_XY_RANGE=lo-hi (hex NES PCs) keeps
        // only contracts inside the range.
        if let Ok(r) = std::env::var("NES2GBC_XY_RANGE") {
            if let Some((lo, hi)) = r.split_once('-') {
                let lo = u16::from_str_radix(lo, 16).unwrap_or(0);
                let hi = u16::from_str_radix(hi, 16).unwrap_or(0xFFFF);
                contracts.retain(|&a, _| a >= lo && a <= hi);
            }
        }
    }
    let header = out;
    let mut carry_iter = 0usize;
    let (mut out, stats, carried) = loop {
    let mut out = header.clone();
    let mut arrivals: BTreeMap<u16, Arrivals> = BTreeMap::new();
    let mut carried_edges = 0usize;
    writeln!(out, "SECTION \"Generated NES reset entry\", ROM0").unwrap();
    out.label("nes_reset");
    out.ins("ld", &["a", "[nes_reset_count]"], None);
    out.ins("inc", &["a"], None);
    out.ins("ld", &["[nes_reset_count]", "a"], None);
    emit_pc_dispatch(&mut out, options.reset);
    writeln!(out).unwrap();

    let selected_list = &plan.order;
    let mut pending_continuation: Option<(u16, u16)> = None;
    let mut section_offs: BTreeMap<u16, usize> = BTreeMap::new();
    let mut rc_offs: BTreeMap<u16, usize> = BTreeMap::new();
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
            rc_offs.clear();
            section_pc = 0;
        }

        // Known-carry facts are block-local: other entries (canonical adapters,
        // dispatch) cannot establish them.
        state.carry = None;
        if continuing {
            entry_contracts.insert(addr, (bank, state));
            out.nes_label(&format!("nes_{addr:04X}_trace"), crate::mir::BlockMeta { pc: addr, bank: None, kind: crate::mir::EntryKind::Trace, poll: poll_points.contains(&addr) && !nmi_exclusive.contains(&addr) });
        } else {
            section_offs.insert(block.start, section_pc);
            out.nes_label(&format!("nes_{:04X}", block.start), crate::mir::BlockMeta { pc: block.start, bank: None, kind: crate::mir::EntryKind::Canonical, poll: poll_points.contains(&block.start) && !nmi_exclusive.contains(&block.start) });
        }
        let contract = if continuing { None } else { contracts.get(&addr).copied().filter(|c| !c.is_empty()) };

        if let Some(c) = contract {
            // HRAM-reload stub for every dynamic/canonical entry (dispatch,
            // RTS/RTI returns, NMI resume, cross-bank and non-carrying
            // predecessors), then the register-contract entry `nes_XXXX_trace`: the same
            // label shape as a chained trace continuation, so every text
            // post-pass already treats it as a resident-register mid-entry.
            // The profile-trace hook moves after it so both entries log.
            let before = out.len();
            // Marker read by text post-passes: with a clean contract HRAM is
            // authoritative on both entries, exactly like a plain block head.
            if c.x != Some(true) && c.y != Some(true) {
                writeln!(out, "    ; xy-carry stub: clean register contract (HRAM authoritative)").unwrap();
            } else {
                writeln!(out, "    ; xy-carry stub: dirty register contract").unwrap();
            }
            if c.y.is_some() {
                out.ins("ldh", &["a", "[nes_y]"], None);
                out.ins("ld", &["c", "a"], Some("carry stub: reload cached X/Y (Y in C)"));
            }
            if c.x.is_some() {
                out.ins("ldh", &["a", "[nes_x]"], None);
                out.ins("ld", &["b", "a"], Some("carry stub: reload cached X/Y (X in B)"));
            }
            section_pc += approx_code_bytes(&out[before..]);
            out.nes_label(&format!("nes_{addr:04X}_trace"), crate::mir::BlockMeta { pc: addr, bank: None, kind: crate::mir::EntryKind::Carry, poll: false });
            rc_offs.insert(addr, section_pc);
            if c.x.is_some() {
                state.x_b = true;
                state.x_dirty = c.x == Some(true);
            }
            if c.y.is_some() {
                state.y_c = true;
                state.y_dirty = c.y == Some(true);
            }
        }

        writeln!(out, "IF DEF(NES2GBC_PROFILE_TRACE)").unwrap();
        if continuing && state.a_live {
            out.ins("push", &["af"], Some("preserve resident A across profile trace"));
        }
        out.ins("ld", &["hl", format!("${:04X}", block.start).as_str()], None);
        out.ins("call", &["nes_profile_trace_pc"], None);
        if continuing && state.a_live {
            out.ins("pop", &["af"], None);
        }
        writeln!(out, "ENDC").unwrap();

        if options.debug_trace {
            let before = out.len();
            if continuing && state.a_live {
                out.ins("push", &["af"], Some("preserve resident A across debug trace"));
            }
            out.ins("ld", &["a", format!("${:02X}", (block.start >> 8) as u8).as_str()], None);
            out.ins("ld", &["[nes_debug_pc_hi]", "a"], None);
            out.ins("ld", &["a", format!("${:02X}", block.start as u8).as_str()], None);
            out.ins("ld", &["[nes_debug_pc_lo]", "a"], None);
            if continuing && state.a_live {
                out.ins("pop", &["af"], None);
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
                out.ins("ldh", &["a", "[nes_host_vblank_pending]"], None);
                out.ins("and", &["a"], None);
                out.ins("jr", &["z", ":+"], None);
                out.ins("ld", &["hl", format!("${:04X}", block.start).as_str()], None);
                out.ins("call", &["nes_poll_nmi_hl"], None);
                out.ins("and", &["a"], None);
                out.ins("jp", &["nz", "nes_nmi_entry"], None);
                out.label("");
                section_pc += approx_code_bytes(&out[before..]);
                if exclusive {
                    writeln!(out, "ENDC").unwrap();
                }
            }
        }

        let mut pending: Vec<IrOp> = Vec::new();
        let write_insn_comment =
            |out: &mut MirBuf, instruction: &crate::cpu6502::DecodedInstruction| {
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
            if options.debug_trace {
                // Debug builds need an exact source-PC breadcrumb, not merely
                // the containing basic-block entry. Do not touch the host stack:
                // a failing program may already be close to/inside stack corruption.
                // LD/LDH preserve GB flags, so only resident A needs a scratch byte.
                // nes_fault_hram is debug-only scratch during normal execution;
                // nes_unimplemented overwrites it with $FF before reporting a fault.
                let before = out.len();
                out.ins("ldh", &["[nes_fault_hram]", "a"], Some("TRACE scratch: preserve resident A"));
                out.ins("ld", &["a", format!("${:02X}", (instruction.pc >> 8) as u8).as_str()], None);
                out.ins("ld", &["[nes_debug_pc_hi]", "a"], None);
                out.ins("ld", &["a", format!("${:02X}", instruction.pc as u8).as_str()], None);
                out.ins("ld", &["[nes_debug_pc_lo]", "a"], None);
                out.ins("ldh", &["a", "[nes_fault_hram]"], Some("restore A; flags preserved"));
                section_pc += approx_code_bytes(&out[before..]);
            }

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

                    let mut probe = MirBuf::new();
                    if emit_static_control(&mut probe, &ops, bank, &banks, None, 0, None) {
                        if !pending.is_empty() {
                            let before = out.len();
                            emit_barrier_ops(&mut out, &pending, &mut state, &mut stats);
                            section_pc += approx_code_bytes(&out[before..]);
                            pending.clear();
                        }
                        let carry_target = match ops.as_slice() {
                            [IrOp::Jump(t)] | [IrOp::Branch { target: t, .. }] => Some(*t),
                            _ => None,
                        };
                        if let Some(t) = carry_target {
                            let before = out.len();
                            let label = carry_exit(&mut out, &mut state, &mut stats, t, bank, &banks, &contracts, &mut arrivals);
                            section_pc += approx_code_bytes(&out[before..]);
                            if let Some(label) = label {
                                carried_edges += 1;
                                write_insn_comment(&mut out, instruction);
                                let before = out.len();
                                if let [IrOp::Branch { flag, when, .. }] = ops.as_slice() {
                                    emit_branch_skip(&mut out, *flag, *when);
                                    let pc_now = section_pc + approx_code_bytes(&out[before..]);
                                    emit_local_jump(&mut out, &label, rc_offs.get(&t).copied(), pc_now);
                                    out.label("");
                                } else {
                                    emit_local_jump(&mut out, &label, rc_offs.get(&t).copied(), section_pc);
                                }
                                section_pc += approx_code_bytes(&out[before..]);
                                continue;
                            }
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
                            None,
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
                        state.v_dead = v_dead.contains(&instruction.pc);
                        state.c_dead = c_dead.contains(&instruction.pc);
                        emit_fast_op(&mut out, &ops[0], &mut state, &mut stats);
                        state.v_dead = false;
                        state.c_dead = false;
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
                    out.ins("ld", &["a", format!("${:02X}", instruction.pc as u8).as_str()], None);
                    out.ins("ldh", &["[nes_fault_pc_lo]", "a"], None);
                    out.ins("ld", &["a", format!("${:02X}", (instruction.pc >> 8) as u8).as_str()], None);
                    out.ins("ldh", &["[nes_fault_pc_hi]", "a"], None);
                    out.ins("jp", &["nes_unimplemented"], None);
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
                    let before = out.len();
                    if let Some(label) = carry_exit(&mut out, &mut state, &mut stats, target, bank, &banks, &contracts, &mut arrivals) {
                        carried_edges += 1;
                        let pc_now = section_pc + approx_code_bytes(&out[before..]);
                        emit_local_jump(&mut out, &label, rc_offs.get(&target).copied(), pc_now);
                        section_pc += approx_code_bytes(&out[before..]);
                    } else {
                        // Unchanged stage-1 accounting (sync bytes not counted).
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
        out.nes_label(&format!("nes_{addr:04X}"), crate::mir::BlockMeta { pc: *addr, bank: None, kind: crate::mir::EntryKind::Canonical, poll: false });
        if contract.x_b {
            out.ins("ldh", &["a", "[nes_x]"], None);
            out.ins("ld", &["b", "a"], Some("canonical adapter X"));
        }
        if contract.y_c {
            out.ins("ldh", &["a", "[nes_y]"], None);
            out.ins("ld", &["c", "a"], Some("canonical adapter Y"));
        }
        if contract.a_live {
            out.ins("ldh", &["a", "[nes_a]"], Some("canonical adapter A"));
        }
        out.ins("jp", &[format!("nes_{addr:04X}_trace").as_str()], None);
        writeln!(out).unwrap();
        stats.canonical_adapters += 1;
    }

    let next = refine_contracts(&contracts, &arrivals);
    if !(carry.x || carry.y) || next == contracts || carry_iter >= 4 {
        break (out, stats, carried_edges);
    }
    contracts = next;
    carry_iter += 1;
    };
    if carry.x || carry.y {
        let nx = contracts.values().filter(|c| c.x.is_some()).count();
        let ny = contracts.values().filter(|c| c.y.is_some()).count();
        println!(
            "xy-carry: {} register-contract entries (X {nx}, Y {ny}), {carried} carried static edge(s), {} refinement pass(es); kept across barriers X {} Y {}",
            contracts.len(),
            carry_iter + 1,
            stats.x_kept_across_barrier,
            stats.y_kept_across_barrier
        );
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
    crate::mir::report_entry_reloads("state_superblock", &out);
    out.into_string()
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
        let banks = assign_code_banks(&graph, &selected, 254);
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
        assert_eq!(between.matches("native RMW shift/rotate").count(), 4);
        assert!(between.contains("sla [hl]") && between.contains("srl [hl]"));
        assert!(between.contains("rl [hl]") && between.contains("rr [hl]"));
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
