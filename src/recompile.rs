use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::fmt::Write;

use crate::{cfg::{ControlFlowGraph, EdgeKind}, ir::{self, Flag, IrOp}, lr35902};

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

fn emit_static_target(
    out: &mut String,
    target: u16,
    current_bank: u16,
    banks: &BTreeMap<u16, u16>,
) -> bool {
    match banks.get(&target).copied() {
        Some(bank) if bank == current_bank => {
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
) -> bool {
    if ops.len() != 1 {
        return false;
    }

    match ops[0] {
        IrOp::Branch { flag, when, target } if banks.contains_key(&target) => {
            writeln!(out, "    ld a, [nes_p]").unwrap();
            writeln!(out, "    and ${:02X}", flag_mask(flag)).unwrap();
            writeln!(out, "    jr {}, :+", if when { "z" } else { "nz" }).unwrap();
            emit_static_target(out, target, current_bank, banks);
            writeln!(out, ":").unwrap();
            true
        }
        IrOp::Jump(target) if banks.contains_key(&target) => {
            emit_static_target(out, target, current_bank, banks);
            true
        }
        IrOp::Call { target, return_addr } if banks.contains_key(&target) => {
            writeln!(out, "    ld hl, ${return_addr:04X}").unwrap();
            writeln!(out, "    call nes_stack_push_return_hl").unwrap();
            emit_static_target(out, target, current_bank, banks);
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
) {
    if !emit_static_target(out, target, current_bank, banks) {
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

pub fn emit_cfg(graph: &ControlFlowGraph, options: EmitOptions) -> String {
    let mut out = String::new();
    writeln!(out, "; Generated by nes2gbc").unwrap();
    writeln!(out, "; Native banked LR35902 output").unwrap();
    writeln!(out).unwrap();

    let limit = options.max_blocks.unwrap_or(graph.blocks.len());
    let selected = select_reachable(graph, options.reset, limit);
    let banks = assign_code_banks(graph, &selected);
    let poll_points = nmi_poll_points(graph, &selected);

    writeln!(out, "SECTION \"Generated NES reset entry\", ROM0").unwrap();
    writeln!(out, "nes_reset:").unwrap();
    emit_pc_dispatch(&mut out, options.reset);
    writeln!(out).unwrap();

    for addr in &selected {
        let block = graph.blocks.get(addr).expect("selected block must exist");
        let bank = banks[addr];

        writeln!(
            out,
            "SECTION \"NES block {addr:04X}\", ROMX, BANK[{bank}]"
        )
        .unwrap();
        writeln!(out, "nes_{:04X}:", block.start).unwrap();

        if options.debug_trace {
            // Keep an exact current NES-PC breadcrumb even when optimized direct
            // jumps bypass the dynamic dispatcher.
            writeln!(out, "    ld a, ${:02X}", (block.start >> 8) as u8).unwrap();
            writeln!(out, "    ld [nes_debug_pc_hi], a").unwrap();
            writeln!(out, "    ld a, ${:02X}", block.start as u8).unwrap();
            writeln!(out, "    ld [nes_debug_pc_lo], a").unwrap();
        }

        if poll_points.contains(&block.start) {
            // Usually there is no pending frame, so loop safe-points pay only
            // a WRAM byte test instead of a helper call and live LY polling.
            writeln!(out, "    ld a, [nes_host_vblank_pending]").unwrap();
            writeln!(out, "    and a").unwrap();
            writeln!(out, "    jr z, :+").unwrap();
            writeln!(out, "    ld hl, ${:04X}", block.start).unwrap();
            writeln!(out, "    call nes_poll_nmi_hl").unwrap();
            writeln!(out, "    and a").unwrap();
            writeln!(out, "    jp nz, nes_nmi_entry").unwrap();
            writeln!(out, ":").unwrap();
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
                Ok(ops) => {
                    if !emit_static_control(&mut out, &ops, bank, &banks) {
                        out.push_str(&lr35902::emit_ops(&ops));
                    }
                }
                Err(err) => {
                    writeln!(out, "    ; TODO {err}").unwrap();
                    writeln!(out, "    jp nes_unimplemented").unwrap();
                    break;
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
                    emit_known_target(&mut out, target, bank, &banks);
                }
            }
        }
        writeln!(out).unwrap();
    }

    emit_dispatch_tables(&mut out, &selected);
    out
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
}
