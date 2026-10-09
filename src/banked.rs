use std::collections::{BTreeMap, BTreeSet};
use std::fmt::Write;

use crate::{
    cfg::{BasicBlock, ControlFlowGraph, EdgeKind},
    ir,
    lr35902,
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
        BlockId::Stub(_) => unreachable!(),
    }
    let name = label(id);
    writeln!(out, "{name}:").unwrap();
    if matches!(id, BlockId::Banked(..)) && bank_stable(block) {
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

    for instruction in &block.instructions {
        writeln!(
            out,
            "    ; ${:04X}: ${:02X} {:?} {:?}",
            instruction.pc,
            instruction.opcode,
            instruction.def.mnemonic,
            instruction.def.mode
        )
        .unwrap();

        match ir::lower_instruction(*instruction) {
            Ok(ops) => out.push_str(&lr35902::emit_ops(&ops)),
            Err(err) => {
                writeln!(out, "    ; TODO {err}").unwrap();
                writeln!(out, "    ld a, ${:02X}", instruction.pc as u8).unwrap();
                writeln!(out, "    ldh [nes_fault_pc_lo], a").unwrap();
                writeln!(out, "    ld a, ${:02X}", (instruction.pc >> 8) as u8).unwrap();
                writeln!(out, "    ldh [nes_fault_pc_hi], a").unwrap();
                writeln!(out, "    jp nes_unimplemented").unwrap();
                writeln!(out).unwrap();
                return;
            }
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
}

fn emit_stub(
    out: &mut String,
    pc: u16,
    variants: &[(u8, u16)],
    host_bank: u16,
) {
    writeln!(
        out,
        "SECTION \"NES mapper2 dispatch stub {pc:04X}\", ROMX, BANK[{host_bank}]"
    )
    .unwrap();
    writeln!(out, "nes_{pc:04X}:").unwrap();
    writeln!(out, "    ld a, [nes_prg_bank]").unwrap();
    for &(bank, _) in variants {
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

    for &(bank, target_bank) in variants {
        let target = label(BlockId::Banked(bank, pc));
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

    let mut variants: BTreeMap<u16, Vec<u8>> = BTreeMap::new();
    for &(bank, pc) in banked_selected.keys() {
        variants.entry(pc).or_default().push(bank);
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
        );
    }

    for (&pc, banks) in &variants {
        let routed: Vec<(u8, u16)> = banks
            .iter()
            .copied()
            .map(|bank| (bank, assigned[&BlockId::Banked(bank, pc)]))
            .collect();
        emit_stub(&mut out, pc, &routed, assigned[&BlockId::Stub(pc)]);
    }

    let mut addresses = BTreeSet::new();
    addresses.extend(fixed_selected.keys().copied());
    addresses.extend(variants.keys().copied());
    emit_dispatch_tables(&mut out, &addresses);

    out
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
