use std::collections::{BTreeMap, BTreeSet};
use std::fmt::Write;

use crate::{
    cfg::{BasicBlock, ControlFlowGraph, EdgeKind},
    recompile::EmitOptions,
};

const DISPATCH_BANK_START: u16 = 32;
const CODE_BANK_START: u16 = 40;
const ESTIMATED_BANK_BUDGET: usize = 0x3000;

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
enum BlockId {
    Fixed(u16),
    Banked(u8, u16),
    Stub(u16),
    /// Ahead-of-time translation of PRG-RAM code (see overlay.rs).
    Overlay(u16),
}

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

fn complete_block(block: &BasicBlock) -> bool {
    let Some(last) = block.instructions.last() else {
        return false;
    };
    terminal_mnemonic(last.def.mnemonic) || !block.edges.is_empty()
}

fn block_cost(block: &BasicBlock) -> usize {
    // Measured raw emitter output averages ~14 bytes per 6502 instruction
    // (MM: 203 KiB for 14950 instructions); keep ~1.7x headroom for
    // expanding post-passes while still fitting 128 KiB commercial games.
    48 + block.instructions.len() * 24
}

fn assign_bank(
    assigned: &mut BTreeMap<BlockId, u16>,
    id: BlockId,
    cost: usize,
    bank: &mut u16,
    used: &mut usize,
) {
    assert!(
        cost <= ESTIMATED_BANK_BUDGET,
        "mapper block is too large for conservative bank packing"
    );
    if *used != 0 && *used + cost > ESTIMATED_BANK_BUDGET {
        *bank += 1;
        *used = 0;
    }
    assert!(*bank <= 254, "translated mapper code exceeds 8-bit MBC5 allocator (bank 255 reserved for runtime helpers)");
    assigned.insert(id, *bank);
    *used += cost;
}

fn poll_points(graph: &ControlFlowGraph) -> BTreeSet<u16> {
    let mut points = BTreeSet::new();
    for &entry in &graph.entry_points {
        points.insert(entry);
    }
    for (&start, block) in &graph.blocks {
        for target in block.edges.iter().filter_map(|edge| edge.target) {
            if target <= start {
                points.insert(target);
            }
        }
    }
    points
}

/// True when no instruction in `block` can write a mapper register
/// ($8000-$FFFF), so the PRG bank mapped at its exit equals the one mapped
/// at its entry. Indirect stores and indexed stores that may reach $8000 are
/// treated as possible bank switches.
fn bank_stable(block: &BasicBlock) -> bool {
    use crate::cpu6502::{AddressingMode::*, Mnemonic::*};
    block.instructions.iter().all(|ins| {
        let writes = match ins.def.mnemonic {
            Sta | Stx | Sty => true,
            Inc | Dec | Asl | Lsr | Rol | Ror => ins.def.mode != Accumulator,
            Brk => return false,
            _ => false,
        };
        if !writes {
            return true;
        }
        match ins.def.mode {
            ZeroPage | ZeroPageX | ZeroPageY => true,
            Absolute => ins.operand < 0x8000,
            AbsoluteX | AbsoluteY => (ins.operand as u32) + 0xFF < 0x8000,
            _ => false,
        }
    })
}

fn label(id: BlockId) -> String {
    match id {
        BlockId::Fixed(pc) | BlockId::Stub(pc) => format!("nes_{pc:04X}"),
        BlockId::Banked(bank, pc) => format!("nes_m2_b{bank:02X}_{pc:04X}"),
        BlockId::Overlay(pc) => format!("nes_r_{pc:04X}"),
    }
}

fn emit_dispatch_tables(out: &mut String, addresses: &BTreeSet<u16>) {
    for segment in 0u16..8 {
        let bank = DISPATCH_BANK_START + segment;
        let base = 0x8000u16 + segment * 0x1000;
        writeln!(
            out,
            "SECTION \"NES dispatch table {segment}\", ROMX[$4000], BANK[{bank}]"
        )
        .unwrap();

        let mut cursor = 0usize;
        let entries: Vec<u16> = if segment == 7 {
            addresses.range(base..=0xFFFF).copied().collect()
        } else {
            addresses.range(base..base + 0x1000).copied().collect()
        };

        for addr in entries {
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

fn emit_poll(out: &mut String, pc: u16) {
    writeln!(out, "    ldh a, [nes_host_vblank_pending]").unwrap();
    writeln!(out, "    and a").unwrap();
    writeln!(out, "    jr z, :+").unwrap();
    writeln!(out, "    ld hl, ${pc:04X}").unwrap();
    writeln!(out, "    call nes_poll_nmi_hl").unwrap();
    writeln!(out, "    and a").unwrap();
    writeln!(out, "    jp nz, nes_nmi_entry").unwrap();
    writeln!(out, ":").unwrap();
}

fn emit_block(
    out: &mut String,
    id: BlockId,
    block: &BasicBlock,
    host_bank: u16,
    do_poll: bool,
    debug_trace: bool,
    expect: Option<&[u8]>,
    shared: bool,
    rts_guard: &[u16],
    v_dead: &BTreeSet<u16>,
    c_dead: &BTreeSet<u16>,
    trace: &mut crate::state_superblock::LocalTrace,
    continuing: bool,
    chain_to: Option<u16>,
) -> Option<String> {
    let mut adapter = None;
    if continuing {
        // Textual continuation of the previous block's trace: no section,
        // a `_trace` label carrying resident registers, and a canonical
        // entry adapter for every other way in (dispatch, guards, stubs).
        let loads = trace.enter_chained();
        let name = label(id);
        adapter = Some(format!("{loads}    jp {name}_trace\n"));
        writeln!(out, "{name}_trace:").unwrap();
        writeln!(out, "IF DEF(NES2GBC_PROFILE_TRACE)").unwrap();
        let a = trace.a_live();
        if a {
            writeln!(out, "    push af").unwrap();
        }
        writeln!(out, "    ld hl, ${:04X}", block.start).unwrap();
        writeln!(out, "    call nes_profile_trace_pc").unwrap();
        if a {
            writeln!(out, "    pop af").unwrap();
        }
        writeln!(out, "ENDC").unwrap();
    } else {
    *trace = Default::default();
    match id {
        BlockId::Fixed(pc) => {
            writeln!(out, "SECTION \"NES block {pc:04X}\", ROMX, BANK[{host_bank}]").unwrap();
        }
        BlockId::Banked(bank, pc) => {
            writeln!(
                out,
                "SECTION \"NES mapper2 b{bank:02X} block {pc:04X}\", ROMX, BANK[{host_bank}]"
            )
            .unwrap();
        }
        BlockId::Overlay(pc) => {
            writeln!(out, "SECTION \"NES overlay block {pc:04X}\", ROMX, BANK[{host_bank}]").unwrap();
        }
        BlockId::Stub(_) => unreachable!(),
    }
    let name = label(id);
    writeln!(out, "{name}:").unwrap();
    if let (BlockId::Overlay(pc), Some(expect)) = (id, expect) {
        emit_overlay_check(out, pc, expect);
        // Unchecked entry for static edges from overlay blocks that cannot
        // store into PRG RAM (`; ovl-pure`): the bytes they were entered
        // with are still intact. NMI resumes and other entries use nes_r_.
        writeln!(out, "nes_r_{pc:04X}_nc:").unwrap();
        if !block_may_write_prg_ram(block) {
            writeln!(out, "    ; ovl-pure").unwrap();
        }
    }
    if matches!(id, BlockId::Banked(..)) && !shared && bank_stable(block) {
        // Read by tools/banked_direct_transfers.py: static exits may enter
        // this view's own variant of a $8000-$BFFF target directly.
        writeln!(out, "    ; m2-bank-stable").unwrap();
    }

    writeln!(out, "IF DEF(NES2GBC_PROFILE_TRACE)").unwrap();
    writeln!(out, "    ld hl, ${:04X}", block.start).unwrap();
    writeln!(out, "    call nes_profile_trace_pc").unwrap();
    writeln!(out, "ENDC").unwrap();

    if debug_trace {
        writeln!(out, "    ld a, ${:02X}", (block.start >> 8) as u8).unwrap();
        writeln!(out, "    ld [nes_debug_pc_hi], a").unwrap();
        writeln!(out, "    ld a, ${:02X}", block.start as u8).unwrap();
        writeln!(out, "    ld [nes_debug_pc_lo], a").unwrap();
    }

    if do_poll {
        emit_poll(out, block.start);
    }
    }

    let body_start = out.len();
    if !crate::state_superblock::emit_block_body_chained(out, &block.instructions, v_dead, c_dead, trace, chain_to) {
        writeln!(out).unwrap();
        return adapter;
    }
    if chain_to.is_some() {
        return adapter;
    }
    if !rts_guard.is_empty() {
        // Guarded RTS continuations: HL holds the architectural return PC
        // after `inc hl`; matching PCs enter their translation directly, the
        // rest take nes_dispatch_hl exactly as before.
        let tail = "    inc hl\n    jp nes_dispatch_hl\n";
        if out[body_start..].ends_with(tail) {
            let mut g = String::from("    inc hl\n");
            for &r in rts_guard {
                writeln!(g, "    ld a, l ; m2 RTS guard ${r:04X}").unwrap();
                writeln!(g, "    cp ${:02X}", r as u8).unwrap();
                writeln!(g, "    jr nz, :+").unwrap();
                writeln!(g, "    ld a, h").unwrap();
                writeln!(g, "    cp ${:02X}", (r >> 8) as u8).unwrap();
                writeln!(g, "    jr nz, :+").unwrap();
                writeln!(g, "    ld a, BANK(nes_{r:04X})").unwrap();
                writeln!(g, "    ld hl, nes_{r:04X}").unwrap();
                writeln!(g, "    jp nes_jump_known_hl_a").unwrap();
                writeln!(g, ":").unwrap();
            }
            g.push_str("    jp nes_dispatch_hl\n");
            let cut = out.len() - tail.len();
            out.truncate(cut);
            out.push_str(&g);
        }
    }

    if let Some(last) = block.instructions.last() {
        if is_branch(last.def.mnemonic) || !terminal_mnemonic(last.def.mnemonic) {
            if let Some(target) = block
                .edges
                .iter()
                .find(|edge| matches!(edge.kind, EdgeKind::Fallthrough))
                .and_then(|edge| edge.target)
            {
                writeln!(out, "    ld hl, ${target:04X}").unwrap();
                writeln!(out, "    jp nes_dispatch_hl").unwrap();
            }
        }
    }
    writeln!(out).unwrap();
    adapter
}

/// Conservatively: may any instruction of the block store to $6000-$7FFF?
fn block_may_write_prg_ram(block: &BasicBlock) -> bool {
    use crate::cpu6502::{AddressingMode as M, Mnemonic::*};
    block.instructions.iter().any(|i| {
        let writes = matches!(i.def.mnemonic, Sta | Stx | Sty | Inc | Dec | Asl | Lsr | Rol | Ror)
            && i.def.mode != M::Accumulator;
        if !writes {
            return false;
        }
        let hits = |lo: u32, hi: u32| lo < 0x8000 && hi >= 0x6000;
        match i.def.mode {
            M::ZeroPage | M::ZeroPageX | M::ZeroPageY => false,
            M::Absolute => hits(i.operand as u32, i.operand as u32),
            M::AbsoluteX | M::AbsoluteY => hits(i.operand as u32, i.operand as u32 + 0xFF),
            _ => true,
        }
    })
}

/// Overlay block entry: confirm PRG RAM still holds the bytes this block was
/// translated from; otherwise interpret from this PC. $6000-$6FFF lives in
/// WRAMX bank 4 and $7000-$7FFF in bank 5, both at $D000.
fn emit_overlay_check(out: &mut String, pc: u16, expect: &[u8]) {
    writeln!(out, "    ldh a, [rSVBK]").unwrap();
    writeln!(out, "    ld b, a").unwrap();
    let mut cur_bank = None;
    for (k, &byte) in expect.iter().enumerate() {
        let addr = pc.wrapping_add(k as u16);
        let wbank = if addr & 0x1000 != 0 { 5 } else { 4 };
        if cur_bank != Some(wbank) {
            writeln!(out, "    ld a, {wbank}").unwrap();
            writeln!(out, "    ldh [rSVBK], a").unwrap();
            writeln!(out, "    ld hl, ${:04X}", 0xD000 | (addr & 0x0FFF)).unwrap();
            cur_bank = Some(wbank);
        }
        writeln!(out, "    ld a, [hli]").unwrap();
        writeln!(out, "    cp ${byte:02X}").unwrap();
        writeln!(out, "    jp nz, .ovl_stale").unwrap();
    }
    writeln!(out, "    ld a, b").unwrap();
    writeln!(out, "    ldh [rSVBK], a").unwrap();
    writeln!(out, "    jr .ovl_ok").unwrap();
    writeln!(out, ".ovl_stale:").unwrap();
    writeln!(out, "    ld a, b").unwrap();
    writeln!(out, "    ldh [rSVBK], a").unwrap();
    writeln!(out, "    ld hl, ${pc:04X}").unwrap();
    writeln!(out, "    jp nes_interp_enter").unwrap();
    writeln!(out, ".ovl_ok:").unwrap();
}

/// PRG-RAM overlay dispatch: HL = NES PC in $6000-$7FFF (others go straight
/// to the interpreter). Two 4 KiB-page tables of (bank, 0, addr) entries.
fn emit_overlay_dispatch(out: &mut String, addresses: &BTreeSet<u16>) {
    writeln!(out, "SECTION \"NES overlay dispatch\", ROM0").unwrap();
    writeln!(out, "nes_overlay_dispatch_hl:").unwrap();
    writeln!(out, "    ld a, h").unwrap();
    writeln!(out, "    cp $60").unwrap();
    writeln!(out, "    jp c, nes_interp_enter").unwrap();
    writeln!(out, "    ld b, h").unwrap();
    writeln!(out, "    ld e, l").unwrap();
    writeln!(out, "    bit 4, a").unwrap();
    writeln!(out, "    ld a, BANK(nes_overlay_table_6)").unwrap();
    writeln!(out, "    jr z, :+").unwrap();
    writeln!(out, "    ld a, BANK(nes_overlay_table_7)").unwrap();
    writeln!(out, ":").unwrap();
    writeln!(out, "    ld [$2000], a").unwrap();
    writeln!(out, "    xor a").unwrap();
    writeln!(out, "    ld [$3000], a").unwrap();
    writeln!(out, "    ld a, h").unwrap();
    writeln!(out, "    and $0F").unwrap();
    writeln!(out, "    ld h, a").unwrap();
    writeln!(out, "    add hl, hl").unwrap();
    writeln!(out, "    add hl, hl").unwrap();
    writeln!(out, "    set 6, h").unwrap();
    writeln!(out, "    ld a, [hli]").unwrap();
    writeln!(out, "    and a").unwrap();
    writeln!(out, "    jr z, .miss").unwrap();
    writeln!(out, "    ld c, a").unwrap();
    writeln!(out, "    inc hl").unwrap();
    writeln!(out, "    ld a, [hli]").unwrap();
    writeln!(out, "    ld h, [hl]").unwrap();
    writeln!(out, "    ld l, a").unwrap();
    writeln!(out, "    ld a, c").unwrap();
    writeln!(out, "    jp nes_jump_known_hl_a").unwrap();
    writeln!(out, ".miss:").unwrap();
    writeln!(out, "    ld h, b").unwrap();
    writeln!(out, "    ld l, e").unwrap();
    writeln!(out, "    jp nes_interp_enter").unwrap();
    writeln!(out).unwrap();
    for page in [6u16, 7] {
        let base = page << 12;
        writeln!(out, "SECTION \"NES overlay dispatch table {page}\", ROMX[$4000]").unwrap();
        writeln!(out, "nes_overlay_table_{page}:").unwrap();
        let mut cursor = 0usize;
        for addr in addresses.range(base..base + 0x1000).copied() {
            let offset = (addr - base) as usize;
            if offset > cursor {
                writeln!(out, "    ds {}, $00", (offset - cursor) * 4).unwrap();
            }
            writeln!(out, "    db BANK(nes_r_{addr:04X}), $00").unwrap();
            writeln!(out, "    dw nes_r_{addr:04X}").unwrap();
            cursor = offset + 1;
        }
        if cursor < 0x1000 {
            writeln!(out, "    ds {}, $00", (0x1000 - cursor) * 4).unwrap();
        }
        writeln!(out).unwrap();
    }
}

fn emit_stub(
    out: &mut String,
    pc: u16,
    variants: &[(u8, u16, u8)],
    host_bank: u16,
) {
    writeln!(
        out,
        "SECTION \"NES mapper2 dispatch stub {pc:04X}\", ROMX, BANK[{host_bank}]"
    )
    .unwrap();
    writeln!(out, "nes_{pc:04X}:").unwrap();
    writeln!(out, "    ld a, [nes_prg_bank]").unwrap();
    for &(bank, _, _) in variants {
        writeln!(out, "    cp ${bank:02X}").unwrap();
        writeln!(out, "    jp z, .m2_b{bank:02X}").unwrap();
    }
    // No translation for the mapped bank: interpret from this PC (the
    // runtime interpreter re-enters translated code at the next transfer).
    writeln!(out, "IF DEF(NES2GBC_RAM_INTERP)").unwrap();
    writeln!(out, "    ld hl, ${pc:04X}").unwrap();
    writeln!(out, "    jp nes_interp_enter_rom").unwrap();
    writeln!(out, "ELSE").unwrap();
    writeln!(out, "    jp nes_unimplemented").unwrap();
    writeln!(out, "ENDC").unwrap();

    for &(bank, target_bank, label_bank) in variants {
        // label_bank != bank: byte-identical code shared from another bank.
        let target = label(BlockId::Banked(label_bank, pc));
        writeln!(out, ".m2_b{bank:02X}:").unwrap();
        writeln!(out, "    ld a, ${target_bank:02X}").unwrap();
        writeln!(out, "    ld hl, {target}").unwrap();
        writeln!(out, "    jp nes_jump_known_hl_a").unwrap();
    }
    writeln!(out).unwrap();
}

/// Emit mapper-2/UxROM code as bank-aware basic blocks.
///
/// Each physical 16 KiB PRG bank is analyzed through a 32 KiB view supplied by
/// the caller. $C000-$FFFF is emitted once from the fixed bank. Every translated
/// $8000-$BFFF entry gets a canonical nes_XXXX dispatcher stub that consults
/// nes_prg_bank before entering the matching physical-bank translation.
///
/// The first version intentionally uses generic PC dispatch at every 6502 basic
/// block boundary. That keeps mapper correctness independent of the aggressive
/// NROM static-edge/superblock optimizer; mapper-specific optimization can layer
/// on after compatibility is proven.
/// REGALLOC>=2/3 for banked builds: backward liveness of one 6502 flag over
/// the translated fixed-bank blocks and $8000-$BFFF bank variants. Same
/// conservative model as state_superblock::flag_dead_after (every executed
/// NMI poll, RTS/RTI/BRK, unresolved, unselected or interpreter-bound edges
/// read the flag), plus the bank rule: a $8000-$BFFF successor is only the
/// same bank's variant when the source is a bank-stable, unshared variant;
/// from fixed code or any other source it is unknown (live).
/// Returns (node, instruction PC) pairs after which the flag is dead.
fn banked_flag_dead(
    fixed: &BTreeMap<u16, BasicBlock>,
    banked: &BTreeMap<(u8, u16), BasicBlock>,
    fixed_polls: &BTreeSet<u16>,
    banked_polls: &BTreeSet<(u8, u16)>,
    shared: &BTreeSet<(u8, u16)>,
    reads: impl Fn(crate::cpu6502::Mnemonic) -> bool,
    writes: impl Fn(crate::cpu6502::Mnemonic) -> bool,
) -> BTreeSet<(BlockId, u16)> {
    use crate::cpu6502::Mnemonic::*;
    let mut nodes: Vec<(BlockId, &BasicBlock, bool)> = Vec::new();
    for (&pc, b) in fixed {
        nodes.push((BlockId::Fixed(pc), b, fixed_polls.contains(&pc)));
    }
    for (&(bank, pc), b) in banked {
        nodes.push((BlockId::Banked(bank, pc), b, banked_polls.contains(&(bank, pc))));
    }
    let succ = |id: BlockId, b: &BasicBlock| -> Option<Vec<BlockId>> {
        // None = some successor is unknown (flag live at exit).
        let last = b.instructions.last()?;
        if matches!(last.def.mnemonic, Rts | Rti | Brk) || b.edges.is_empty() {
            return None;
        }
        let stable_bank = match id {
            BlockId::Banked(bank, pc) if bank_stable(b) && !shared.contains(&(bank, pc)) => Some(bank),
            _ => None,
        };
        let mut v = Vec::new();
        for e in &b.edges {
            let t = e.target?;
            if t >= 0xC000 {
                if !fixed.contains_key(&t) {
                    return None;
                }
                v.push(BlockId::Fixed(t));
            } else if t >= 0x8000 {
                let bank = stable_bank?;
                if !banked.contains_key(&(bank, t)) {
                    return None;
                }
                v.push(BlockId::Banked(bank, t));
            } else {
                return None;
            }
        }
        Some(v)
    };
    let succs: Vec<Option<Vec<BlockId>>> = nodes.iter().map(|(id, b, _)| succ(*id, b)).collect();
    let index: BTreeMap<BlockId, usize> = nodes.iter().enumerate().map(|(i, (id, _, _))| (*id, i)).collect();
    let through = |b: &BasicBlock, mut live: bool| -> bool {
        for insn in b.instructions.iter().rev() {
            let m = insn.def.mnemonic;
            if writes(m) {
                live = false;
            }
            if reads(m) {
                live = true;
            }
        }
        live
    };
    let mut live_in = vec![false; nodes.len()];
    let live_out = |i: usize, live_in: &Vec<bool>| -> bool {
        match &succs[i] {
            None => true,
            Some(v) => v.iter().any(|t| live_in[index[t]]),
        }
    };
    loop {
        let mut changed = false;
        for i in (0..nodes.len()).rev() {
            if live_in[i] {
                continue;
            }
            let (_, b, polled) = nodes[i];
            if through(b, live_out(i, &live_in)) || polled {
                live_in[i] = true;
                changed = true;
            }
        }
        if !changed {
            break;
        }
    }
    let mut dead = BTreeSet::new();
    for (i, (id, b, _)) in nodes.iter().enumerate() {
        let mut live = live_out(i, &live_in);
        for insn in b.instructions.iter().rev() {
            if !live {
                dead.insert((*id, insn.pc));
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

pub fn emit_mapper2_cfgs(
    views: &[(u8, ControlFlowGraph)],
    options: EmitOptions,
    overlay: Option<&crate::overlay::RamOverlay>,
    prg: &[u8],
) -> String {
    assert!(!views.is_empty(), "mapper 2 requires at least one PRG bank view");

    let mut fixed: BTreeMap<u16, BasicBlock> = BTreeMap::new();
    let mut banked: BTreeMap<(u8, u16), BasicBlock> = BTreeMap::new();
    let mut fixed_polls = BTreeSet::new();
    let mut banked_polls = BTreeSet::new();

    // Fixed-bank ($C000+) poll points mirror the NROM rule (vectors, PHA
    // continuations, harvested jump-table targets, backward-edge targets).
    // Every fixed block is a discovery seed in every view, so the plain
    // entry-point rule would poll at every fixed block.
    let vec_at = |o: usize| u16::from_le_bytes([prg[prg.len() - o], prg[prg.len() - o + 1]]);
    let vectors = [vec_at(6), vec_at(4), vec_at(2), options.reset];
    let poll_all_fixed = std::env::var("NES2GBC_BANKED_FIXED_POLL_ALL").is_ok();
    for (bank, graph) in views {
        let mut polls = poll_points(graph);
        if !poll_all_fixed {
            let mut fixed_points: BTreeSet<u16> = vectors.iter().copied().collect();
            fixed_points.extend(graph.dynamic_entries.iter().copied());
            for (&start, block) in &graph.blocks {
                for t in block.edges.iter().filter_map(|e| e.target) {
                    if t <= start {
                        fixed_points.insert(t);
                    }
                }
            }
            polls.retain(|&pc| pc < 0xC000 || fixed_points.contains(&pc));
        }
        for (&pc, block) in &graph.blocks {
            // Cross-bank convergence intentionally probes addresses that may be
            // data in some physical banks. CFG records a failed probe as an
            // empty or unterminated block; never expose those through dispatch.
            if !complete_block(block) {
                continue;
            }
            if pc >= 0xC000 {
                fixed.entry(pc).or_insert_with(|| block.clone());
                if polls.contains(&pc) {
                    fixed_polls.insert(pc);
                }
            } else if pc >= 0x8000 {
                banked.insert((*bank, pc), block.clone());
                if polls.contains(&pc) {
                    banked_polls.insert((*bank, pc));
                }
            }
        }
    }

    if std::env::var("NES2GBC_CFG_DEBUG").is_ok() {
        let fi: usize = fixed.values().map(|b| b.instructions.len()).sum();
        let bi: usize = banked.values().map(|b| b.instructions.len()).sum();
        eprintln!("banked-cfg: fixed {} blocks/{} insns, banked {} blocks/{} insns", fixed.len(), fi, banked.len(), bi);
        for (bank, _) in views {
            let n: usize = banked.iter().filter(|((b, _), _)| b == bank).map(|(_, blk)| blk.instructions.len()).sum();
            let k = banked.keys().filter(|(b, _)| b == bank).count();
            eprintln!("  bank {bank}: {k} blocks, {n} insns");
        }
    }
    let limit = options
        .max_blocks
        .unwrap_or(fixed.len().saturating_add(banked.len()));

    let mut fixed_selected = BTreeMap::new();
    let mut banked_selected = BTreeMap::new();
    let mut remaining = limit;
    for (&pc, block) in &fixed {
        if remaining == 0 {
            break;
        }
        fixed_selected.insert(pc, block.clone());
        remaining -= 1;
    }
    if remaining != 0 {
        for (&key, block) in &banked {
            if remaining == 0 {
                break;
            }
            banked_selected.insert(key, block.clone());
            remaining -= 1;
        }
    }

    // Share translations across banks holding byte-identical code (e.g. a
    // bank-switch routine duplicated at the same address in every bank and
    // called from code that runs with any bank mapped).
    let (aliases, shared_canon) = identical_bank_aliases(views, &banked_selected, prg);
    let mut variants: BTreeMap<u16, Vec<(u8, u8)>> = BTreeMap::new();
    for &(bank, pc) in banked_selected.keys() {
        variants.entry(pc).or_default().push((bank, bank));
    }
    for (&(bank, pc), &canon) in &aliases {
        variants.entry(pc).or_default().push((bank, canon));
    }
    for v in variants.values_mut() {
        v.sort();
    }
    if !aliases.is_empty() {
        println!(
            "mapper2: {} byte-identical cross-bank alias(es) share {} translated block(s)",
            aliases.len(),
            shared_canon.len()
        );
    }

    // Fixed-bank traces: P -> B where B is P's fallthrough/JMP successor,
    // a translated fixed block with exactly one static predecessor, not an
    // entry point and not an NMI poll point. B is emitted textually after P
    // in the same section with A/X/Y residency carried across; every other
    // entry to B goes through a canonical adapter.
    let mut chain_next: BTreeMap<u16, u16> = BTreeMap::new();
    if !options.debug_trace && std::env::var("NES2GBC_BANKED_TRACES").map(|v| v == "1").unwrap_or(false) {
        let mut incoming: BTreeMap<u16, usize> = BTreeMap::new();
        let mut count = |b: &BasicBlock| {
            for t in b.edges.iter().filter_map(|e| e.target) {
                *incoming.entry(t).or_default() += 1;
            }
        };
        fixed_selected.values().for_each(&mut count);
        banked_selected.values().for_each(&mut count);
        if let Some(ov) = overlay {
            ov.blocks.values().for_each(&mut count);
        }
        let mut entries: BTreeSet<u16> = BTreeSet::new();
        entries.insert(options.reset);
        let mut claimed: BTreeSet<u16> = BTreeSet::new();
        for (&pc, block) in &fixed_selected {
            let Some((t, _)) = crate::state_superblock::preferred_successor(block) else { continue };
            // A JSR's continuation is entered by the subroutine's RTS through
            // dispatch (the canonical adapter), never by falling through.
            if block.instructions.last().is_some_and(|i| i.def.mnemonic == crate::cpu6502::Mnemonic::Jsr) {
                continue;
            }
            if t < 0xC000
                || t == pc
                || !fixed_selected.contains_key(&t)
                || incoming.get(&t).copied().unwrap_or(0) != 1
                || entries.contains(&t)
                || fixed_polls.contains(&t)
                || claimed.contains(&t)
            {
                continue;
            }
            claimed.insert(t);
            chain_next.insert(pc, t);
        }
        // Break cycles (a ring of unique-entry blocks would have no head).
        let heads: Vec<u16> = fixed_selected.keys().copied().filter(|pc| !claimed.contains(pc)).collect();
        let mut reached: BTreeSet<u16> = BTreeSet::new();
        for h in heads {
            let mut c = h;
            while let Some(&n) = chain_next.get(&c) {
                reached.insert(n);
                c = n;
            }
        }
        chain_next.retain(|_, t| reached.contains(t));
        if let Some(lim) = std::env::var("NES2GBC_BANKED_TRACES_RANGE").ok() {
            let (a, b) = lim.split_once('-').unwrap();
            let (a, b): (usize, usize) = (a.parse().unwrap(), b.parse().unwrap());
            let keep: BTreeSet<u16> = chain_next.keys().copied().enumerate().filter(|(i, _)| *i >= a && *i < b).map(|(_, k)| k).collect();
            chain_next.retain(|k, _| keep.contains(k));
        }
        println!("banked-traces: chained {} fixed-bank edge(s) (fixed {}, polled {}, unique-entry fixed {})", chain_next.len(), fixed_selected.len(), fixed_polls.len(), fixed_selected.keys().filter(|p| incoming.get(p).copied().unwrap_or(0) == 1).count());
    }
    let chained: BTreeSet<u16> = chain_next.values().copied().collect();
    let mut fixed_order: Vec<u16> = Vec::new();
    for &pc in fixed_selected.keys() {
        if chained.contains(&pc) {
            continue;
        }
        let mut c = pc;
        fixed_order.push(c);
        while let Some(&n) = chain_next.get(&c) {
            fixed_order.push(n);
            c = n;
        }
    }

    let mut assigned = BTreeMap::new();
    let mut host_bank = CODE_BANK_START;
    let mut used = 0usize;
    {
        let mut i = 0;
        while i < fixed_order.len() {
            let head = fixed_order[i];
            let mut cost = block_cost(&fixed_selected[&head]);
            let mut j = i + 1;
            while j < fixed_order.len() && chained.contains(&fixed_order[j]) {
                cost += block_cost(&fixed_selected[&fixed_order[j]]);
                j += 1;
            }
            assign_bank(&mut assigned, BlockId::Fixed(head), cost, &mut host_bank, &mut used);
            let hb = assigned[&BlockId::Fixed(head)];
            for k in i + 1..j {
                assigned.insert(BlockId::Fixed(fixed_order[k]), hb);
            }
            i = j;
        }
    }
    let align_views = std::env::var("NES2GBC_ALIGN_VIEW_BANKS").map(|v| v == "1").unwrap_or(false);
    let mut last_view: Option<u8> = None;
    for (&(bank, pc), block) in &banked_selected {
        // Optionally start each PRG bank's variants in a fresh host bank so
        // the final whole-bank repack keeps a view's hot code together.
        if align_views && last_view != Some(bank) && used != 0 {
            host_bank += 1;
            used = 0;
        }
        last_view = Some(bank);
        assign_bank(
            &mut assigned,
            BlockId::Banked(bank, pc),
            block_cost(block),
            &mut host_bank,
            &mut used,
        );
    }
    for (&pc, banks) in &variants {
        assign_bank(
            &mut assigned,
            BlockId::Stub(pc),
            48 + banks.len() * 18,
            &mut host_bank,
            &mut used,
        );
    }
    if let Some(ov) = overlay {
        for (&pc, block) in &ov.blocks {
            let bytes: usize = block.instructions.iter().map(|i| i.def.len() as usize).sum();
            assign_bank(
                &mut assigned,
                BlockId::Overlay(pc),
                block_cost(block) + 24 + bytes * 6,
                &mut host_bank,
                &mut used,
            );
        }
        println!(
            "ram-overlay: translated {} block(s) of PRG-RAM code at ${:04X}-${:04X}",
            ov.blocks.len(),
            ov.dst,
            ov.dst as usize + ov.bytes.len() - 1
        );
    }

    println!(
        "mapper2: emitted {} fixed block(s), {} banked block variant(s), {} bank-aware dispatch stub(s) across {} PRG bank view(s)",
        fixed_selected.len(),
        banked_selected.len(),
        variants.len(),
        views.len()
    );

    // RTS guard targets must be dispatchable labels (fixed block or stub).
    let mut labels: BTreeSet<u16> = fixed_selected.keys().copied().collect();
    labels.extend(variants.keys().copied());
    let max_guards: usize = std::env::var("NES2GBC_RTS_GUARDS").ok().and_then(|v| v.parse().ok()).unwrap_or(6);
    let rts_guards: BTreeMap<BlockId, Vec<u16>> = rts_return_candidates(views)
        .into_iter()
        .map(|(k, v)| (k, v.into_iter().filter(|r| labels.contains(r)).take(max_guards).collect::<Vec<u16>>()))
        .filter(|(_, v)| !v.is_empty())
        .collect();
    println!("mapper2: guarded RTS continuations in {} block(s)", rts_guards.len());
    let guard_for = |id: BlockId| -> &[u16] { rts_guards.get(&id).map(|v| v.as_slice()).unwrap_or(&[]) };

    let level = crate::state_superblock::regalloc_level();
    let v_dead = if level >= 2 {
        use crate::cpu6502::Mnemonic::*;
        banked_flag_dead(&fixed_selected, &banked_selected, &fixed_polls, &banked_polls, &shared_canon,
            |m| matches!(m, Bvc | Bvs | Php | Brk),
            |m| matches!(m, Adc | Sbc | Bit | Clv | Plp | Rti))
    } else {
        BTreeSet::new()
    };
    let c_dead = if level >= 3 {
        use crate::cpu6502::Mnemonic::*;
        banked_flag_dead(&fixed_selected, &banked_selected, &fixed_polls, &banked_polls, &shared_canon,
            |m| matches!(m, Adc | Sbc | Rol | Ror | Bcc | Bcs | Php | Brk),
            |m| matches!(m, Adc | Sbc | Cmp | Cpx | Cpy | Asl | Lsr | Rol | Ror | Clc | Sec | Plp | Rti))
    } else {
        BTreeSet::new()
    };
    if level >= 2 {
        println!("banked-regalloc: V dead after {} instruction(s), C dead after {}", v_dead.len(), c_dead.len());
    }
    let dead_for = |id: BlockId, set: &BTreeSet<(BlockId, u16)>| -> BTreeSet<u16> {
        set.range((id, 0)..=(id, 0xFFFF)).map(|&(_, pc)| pc).collect()
    };

    let mut out = String::new();
    writeln!(out, "; Generated by nes2gbc mapper-2 banked emitter").unwrap();
    writeln!(out, "; Correctness-first UxROM basic-block dispatch").unwrap();
    writeln!(out).unwrap();

    writeln!(out, "SECTION \"Generated NES reset entry\", ROM0").unwrap();
    writeln!(out, "nes_reset:").unwrap();
    writeln!(out, "    ld a, [nes_reset_count]").unwrap();
    writeln!(out, "    inc a").unwrap();
    writeln!(out, "    ld [nes_reset_count], a").unwrap();
    writeln!(out, "    ld hl, ${:04X}", options.reset).unwrap();
    writeln!(out, "    jp nes_dispatch_hl").unwrap();
    writeln!(out).unwrap();

    let mut trace = crate::state_superblock::LocalTrace::default();
    let mut adapters: Vec<(BlockId, String)> = Vec::new();
    let mut prev_chain: Option<u16> = None;
    for &pc in &fixed_order {
        let block = &fixed_selected[&pc];
        let continuing = prev_chain == Some(pc);
        prev_chain = chain_next.get(&pc).copied();
        let adapter = emit_block(
            &mut out,
            BlockId::Fixed(pc),
            block,
            assigned[&BlockId::Fixed(pc)],
            fixed_polls.contains(&pc),
            options.debug_trace,
            None,
            false,
            guard_for(BlockId::Fixed(pc)),
            &dead_for(BlockId::Fixed(pc), &v_dead),
            &dead_for(BlockId::Fixed(pc), &c_dead),
            &mut trace,
            continuing,
            prev_chain,
        );
        if let Some(a) = adapter {
            adapters.push((BlockId::Fixed(pc), a));
        }
    }
    for (id, a) in &adapters {
        let name = label(*id);
        let BlockId::Fixed(apc) = *id else { unreachable!() };
        // Same section name as the NROM emitter's adapters so the final
        // repack keeps adapter jumps (`jp nes_X_trace`) bank-correct.
        writeln!(out, "SECTION \"NES canonical superblock entry {apc:04X}\", ROMX, BANK[{}]", assigned[id]).unwrap();
        writeln!(out, "{name}:").unwrap();
        out.push_str(a);
        writeln!(out).unwrap();
    }
    {
    }
    for (&(bank, pc), block) in &banked_selected {
        emit_block(
            &mut out,
            BlockId::Banked(bank, pc),
            block,
            assigned[&BlockId::Banked(bank, pc)],
            banked_polls.contains(&(bank, pc)),
            options.debug_trace,
            None,
            shared_canon.contains(&(bank, pc)),
            guard_for(BlockId::Banked(bank, pc)),
            &dead_for(BlockId::Banked(bank, pc), &v_dead),
            &dead_for(BlockId::Banked(bank, pc), &c_dead),
            &mut crate::state_superblock::LocalTrace::default(),
            false,
            None,
        );
    }
    if let Some(ov) = overlay {
        for (&pc, block) in &ov.blocks {
            let len: usize = block.instructions.iter().map(|i| i.def.len() as usize).sum();
            let start = (pc - ov.dst) as usize;
            emit_block(
                &mut out,
                BlockId::Overlay(pc),
                block,
                assigned[&BlockId::Overlay(pc)],
                true,
                options.debug_trace,
                Some(&ov.bytes[start..start + len]),
                false,
                &[],
                &BTreeSet::new(),
                &BTreeSet::new(),
                &mut crate::state_superblock::LocalTrace::default(),
                false,
                None,
            );
        }
        let entries: BTreeSet<u16> = ov.blocks.keys().copied().collect();
        emit_overlay_dispatch(&mut out, &entries);
    }

    for (&pc, banks) in &variants {
        let routed: Vec<(u8, u16, u8)> = banks
            .iter()
            .copied()
            .map(|(bank, canon)| (bank, assigned[&BlockId::Banked(canon, pc)], canon))
            .collect();
        emit_stub(&mut out, pc, &routed, assigned[&BlockId::Stub(pc)]);
    }

    let mut addresses = BTreeSet::new();
    addresses.extend(fixed_selected.keys().copied());
    addresses.extend(variants.keys().copied());
    emit_dispatch_tables(&mut out, &addresses);

    out
}

/// Likely RTS continuations per RTS-terminated block: the return sites
/// (JSR pc+3) of every subroutine entry whose intra-procedural flow reaches
/// the block, most-called first. Used only as guarded direct-jump hints; the
/// popped address is compared exactly and anything else still dispatches.
fn rts_return_candidates(views: &[(u8, ControlFlowGraph)]) -> BTreeMap<BlockId, Vec<u16>> {
    const MAX_WALK: usize = 600;
    let key = |bank: u8, pc: u16| if pc >= 0xC000 { BlockId::Fixed(pc) } else { BlockId::Banked(bank, pc) };
    // Return sites per callee entry, counted once per distinct caller block.
    let mut sites: BTreeMap<u16, BTreeMap<u16, BTreeSet<BlockId>>> = BTreeMap::new();
    for (bank, g) in views {
        for (&pc, b) in &g.blocks {
            let callee = b.edges.iter().find(|e| e.kind == EdgeKind::Call).and_then(|e| e.target);
            let ret = b.edges.iter().find(|e| e.kind == EdgeKind::CallReturn).and_then(|e| e.target);
            if let (Some(c), Some(r)) = (callee, ret) {
                sites.entry(c).or_default().entry(r).or_default().insert(key(*bank, pc));
            }
        }
    }
    // Rank by the shortest intra-procedural distance from the subroutine
    // entry (the RTS's own routine first), then by static caller count.
    let mut acc: BTreeMap<BlockId, BTreeMap<u16, (usize, usize)>> = BTreeMap::new();
    for (bank, g) in views {
        for (&entry, rets) in &sites {
            if !g.blocks.contains_key(&entry) {
                continue;
            }
            let mut seen = BTreeSet::from([entry]);
            let mut work = std::collections::VecDeque::from([(entry, 0usize)]);
            while let Some((pc, dist)) = work.pop_front() {
                if seen.len() > MAX_WALK {
                    break;
                }
                let Some(b) = g.blocks.get(&pc) else { continue };
                if b.instructions.last().map(|i| i.def.mnemonic) == Some(crate::cpu6502::Mnemonic::Rts) {
                    let slot = acc.entry(key(*bank, pc)).or_default();
                    for (&r, callers) in rets {
                        let e = slot.entry(r).or_insert((usize::MAX, 0));
                        e.0 = e.0.min(dist);
                        e.1 += callers.len();
                    }
                }
                for e in &b.edges {
                    if matches!(e.kind, EdgeKind::Fallthrough | EdgeKind::BranchTaken | EdgeKind::Jump | EdgeKind::CallReturn) {
                        if let Some(t) = e.target {
                            if seen.insert(t) {
                                work.push_back((t, dist + 1));
                            }
                        }
                    }
                }
            }
        }
    }
    acc.into_iter()
        .map(|(k, m)| {
            let mut v: Vec<(u16, (usize, usize))> = m.into_iter().collect();
            v.sort_by(|a, b| a.1 .0.cmp(&b.1 .0).then(b.1 .1.cmp(&a.1 .1)).then(a.0.cmp(&b.0)));
            (k, v.into_iter().map(|(r, _)| r).collect())
        })
        .collect()
}

/// `(bank, pc) -> canonical bank` for $8000-$BFFF code with no translation in
/// `bank` whose whole low-window closure (every block reachable from it
/// without leaving $8000-$BFFF) is byte-identical to a translated closure in
/// the canonical bank. Also returns the canonical blocks involved: they must
/// not assume their own bank at exit (no `m2-bank-stable` direct variants).
fn identical_bank_aliases(
    views: &[(u8, ControlFlowGraph)],
    banked: &BTreeMap<(u8, u16), BasicBlock>,
    prg: &[u8],
) -> (BTreeMap<(u8, u16), u8>, BTreeSet<(u8, u16)>) {
    const MAX_CLOSURE: usize = 64;
    let bank_count = prg.len() / 0x4000;
    let mut aliases: BTreeMap<(u8, u16), u8> = BTreeMap::new();
    let mut shared = BTreeSet::new();
    if bank_count < 3 {
        return (aliases, shared);
    }
    let range = |b: &BasicBlock| -> (usize, usize) {
        let lo = (b.start as usize) & 0x3FFF;
        let hi = b
            .instructions
            .last()
            .map(|i| ((i.pc as usize) & 0x3FFF) + i.def.len() as usize)
            .unwrap_or(lo);
        (lo, hi.min(0x4000))
    };
    for (bank, g) in views {
        let bank = *bank;
        if bank as usize >= bank_count - 1 {
            continue; // fixed bank's low view: never mapped at $8000 by mode 3/UxROM
        }
        for &(b, pc) in banked.keys() {
            if b != bank {
                continue;
            }
            // Low-window closure from pc in this bank.
            let mut closure = vec![pc];
            let mut seen: BTreeSet<u16> = closure.iter().copied().collect();
            let mut k = 0;
            let mut ok = true;
            while k < closure.len() {
                let Some(blk) = banked.get(&(bank, closure[k])) else { ok = false; break };
                for t in blk.edges.iter().filter_map(|e| e.target) {
                    if (0x8000..0xC000).contains(&t) && seen.insert(t) {
                        if !g.blocks.contains_key(&t) {
                            ok = false;
                        }
                        closure.push(t);
                    }
                }
                if blk.edges.iter().any(|e| e.target.is_none()) {
                    ok = false; // unresolved indirect: cannot prove the closure
                }
                k += 1;
                if closure.len() > MAX_CLOSURE {
                    ok = false;
                }
                if !ok {
                    break;
                }
            }
            if !ok {
                continue;
            }
            for other in 0..(bank_count - 1) as u8 {
                if other == bank || banked.contains_key(&(other, pc)) || aliases.contains_key(&(other, pc)) {
                    continue;
                }
                let identical = closure.iter().all(|t| {
                    let blk = &banked[&(bank, *t)];
                    let (lo, hi) = range(blk);
                    prg[bank as usize * 0x4000 + lo..bank as usize * 0x4000 + hi]
                        == prg[other as usize * 0x4000 + lo..other as usize * 0x4000 + hi]
                });
                if !identical {
                    continue;
                }
                for &t in &closure {
                    if !banked.contains_key(&(other, t)) {
                        aliases.entry((other, t)).or_insert(bank);
                        shared.insert((bank, t));
                    }
                }
            }
        }
    }
    // Only canonicals actually used by an alias lose their bank-stable marker.
    let used: BTreeSet<(u8, u16)> = aliases.iter().map(|(&(_, pc), &c)| (c, pc)).collect();
    shared.retain(|k| used.contains(k));
    (aliases, shared)
}

/// Low-window targets reached right after a constant UxROM bank select in the
/// same basic block, e.g. `LDA #6 / STA $C006 / JSR $BFF6`. Returns
/// `(selected_bank, target)` pairs so the caller can analyze `target` in the
/// physical bank that will actually be mapped when control arrives.
pub fn constant_bank_switch_targets(graph: &ControlFlowGraph, bank_mask: u8) -> Vec<(u8, u16)> {
    use crate::cpu6502::{AddressingMode, Mnemonic::*};
    let mut out = Vec::new();
    for block in graph.blocks.values() {
        let (mut a, mut x, mut y): (Option<u8>, Option<u8>, Option<u8>) = (None, None, None);
        let mut selected: Option<u8> = None;
        for ins in &block.instructions {
            let m = ins.def.mnemonic;
            let mode = ins.def.mode;
            match m {
                Lda if mode == AddressingMode::Immediate => a = Some(ins.operand as u8),
                Ldx if mode == AddressingMode::Immediate => x = Some(ins.operand as u8),
                Ldy if mode == AddressingMode::Immediate => y = Some(ins.operand as u8),
                Tax => x = a,
                Tay => y = a,
                Txa => a = x,
                Tya => a = y,
                Sta | Stx | Sty => {
                    if mode == AddressingMode::Absolute && ins.operand >= 0x8000 {
                        let v = match m {
                            Sta => a,
                            Stx => x,
                            _ => y,
                        };
                        selected = v.map(|v| v & bank_mask);
                    }
                }
                Clc | Sec | Cli | Sei | Cld | Sed | Clv | Nop | Pha | Php => {}
                Jmp | Jsr if mode == AddressingMode::Absolute => {
                    if let Some(bank) = selected {
                        if (0x8000..0xC000).contains(&ins.operand) {
                            out.push((bank, ins.operand));
                        }
                    }
                }
                Lda | Pla | Adc | Sbc | And | Ora | Eor => a = None,
                Ldx | Inx | Dex | Tsx => x = None,
                Ldy | Iny | Dey => y = None,
                Asl | Lsr | Rol | Ror if mode == AddressingMode::Accumulator => a = None,
                _ => {}
            }
        }
    }
    out
}
