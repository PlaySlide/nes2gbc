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
) {
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

    if !crate::state_superblock::emit_block_body_local(out, &block.instructions) {
        writeln!(out).unwrap();
        return;
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

    for (bank, graph) in views {
        let polls = poll_points(graph);
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

    let mut assigned = BTreeMap::new();
    let mut host_bank = CODE_BANK_START;
    let mut used = 0usize;
    for (&pc, block) in &fixed_selected {
        assign_bank(
            &mut assigned,
            BlockId::Fixed(pc),
            block_cost(block),
            &mut host_bank,
            &mut used,
        );
    }
    for (&(bank, pc), block) in &banked_selected {
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

    for (&pc, block) in &fixed_selected {
        emit_block(
            &mut out,
            BlockId::Fixed(pc),
            block,
            assigned[&BlockId::Fixed(pc)],
            fixed_polls.contains(&pc),
            options.debug_trace,
            None,
            false,
        );
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
