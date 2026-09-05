use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::fmt::Write;

use crate::{cfg::{ControlFlowGraph, EdgeKind}, cpu6502::{AddressingMode, DecodedInstruction, Mnemonic}, ir::{self, Flag, IrOp}, lr35902};

#[derive(Debug, Clone, Copy)]
pub struct EmitOptions {
    pub reset: u16,
    pub max_blocks: Option<usize>,
}

impl Default for EmitOptions {
    fn default() -> Self {
        Self { reset: 0x8000, max_blocks: Some(64) }
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

const NES_FRAME_CPU_CYCLES: u16 = 29_780;

fn instruction_base_cycles(i: DecodedInstruction) -> u16 {
    use AddressingMode::*;
    use Mnemonic::*;

    match i.def.mnemonic {
        Brk => 7,
        Jsr => 6,
        Rti | Rts => 6,
        Jmp if i.def.mode == Indirect => 5,
        Jmp => 3,

        Bcc | Bcs | Beq | Bmi | Bne | Bpl | Bvc | Bvs => 2,

        Pha | Php => 3,
        Pla | Plp => 4,

        Asl | Lsr | Rol | Ror => match i.def.mode {
            Accumulator => 2,
            ZeroPage => 5,
            ZeroPageX => 6,
            Absolute => 6,
            AbsoluteX => 7,
            _ => 2,
        },

        Inc | Dec => match i.def.mode {
            ZeroPage => 5,
            ZeroPageX => 6,
            Absolute => 6,
            AbsoluteX => 7,
            _ => 2,
        },

        Sta | Stx | Sty => match i.def.mode {
            ZeroPage => 3,
            ZeroPageX | ZeroPageY => 4,
            Absolute => 4,
            AbsoluteX | AbsoluteY => 5,
            IndexedIndirect | IndirectIndexed => 6,
            _ => 2,
        },

        Bit => match i.def.mode {
            ZeroPage => 3,
            Absolute => 4,
            _ => 2,
        },

        Lda | Ldx | Ldy | And | Ora | Eor | Adc | Sbc | Cmp | Cpx | Cpy => match i.def.mode {
            Immediate => 2,
            ZeroPage => 3,
            ZeroPageX | ZeroPageY => 4,
            Absolute => 4,
            AbsoluteX | AbsoluteY => 4,
            IndexedIndirect => 6,
            IndirectIndexed => 5,
            _ => 2,
        },

        Clc | Cld | Cli | Clv | Dex | Dey | Inx | Iny | Nop | Sec | Sed | Sei |
        Tax | Tay | Tsx | Txa | Txs | Tya => 2,
    }
}

fn block_base_cycles(block: &crate::cfg::BasicBlock) -> u16 {
    block.instructions.iter()
        .map(|&i| instruction_base_cycles(i))
        .fold(0u16, u16::saturating_add)
        .max(1)
}

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

pub fn emit_cfg(graph: &ControlFlowGraph, options: EmitOptions) -> String {
    let mut out = String::new();
    writeln!(out, "; Generated by nes2gbc").unwrap();
    writeln!(out, "; Native banked LR35902 output").unwrap();
    writeln!(out).unwrap();

    let limit = options.max_blocks.unwrap_or(graph.blocks.len());
    let selected = select_reachable(graph, options.reset, limit);
    let banks = assign_code_banks(graph, &selected);

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

        let block_cycles = block_base_cycles(block);
        writeln!(out, "    ld a, [nes_nmi_active]").unwrap();
        writeln!(out, "    and a").unwrap();
        writeln!(out, "    jr nz, :+").unwrap();
        writeln!(out, "    ld hl, ${:04X}", block.start).unwrap();
        writeln!(out, "    ld bc, ${block_cycles:04X}").unwrap();
        writeln!(out, "    call nes_poll_nmi_hl").unwrap();
        writeln!(out, "    and a").unwrap();
        writeln!(out, "    jp nz, nes_nmi_entry").unwrap();
        writeln!(out, ":").unwrap();

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
    fn nes_frame_cycle_budget_matches_ntsc_cpu_rate() {
        assert_eq!(NES_FRAME_CPU_CYCLES, 29_780);
    }

    #[test]
    fn reset_entry_dispatches_into_banked_code() {
        let mut prg = vec![0xEA; 0x8000];
        prg[0x1000] = 0x60;
        let graph = cfg::discover(0, &prg, &[0x9000]).unwrap();
        let asm = emit_cfg(&graph, EmitOptions { reset: 0x9000, max_blocks: Some(1) });
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
        let asm = emit_cfg(&graph, EmitOptions { reset: 0x8000, max_blocks: Some(3) });
        assert!(asm.contains("jp nes_8004") || asm.contains("jp nes_8002"));
    }

    #[test]
    fn dispatch_table_handles_ffff_without_u16_overflow() {
        let mut prg = vec![0xEA; 0x8000];
        prg[0x7FFF] = 0x60;
        let graph = cfg::discover(0, &prg, &[0xFFFF]).unwrap();
        let asm = emit_cfg(&graph, EmitOptions { reset: 0xFFFF, max_blocks: Some(1) });
        assert!(asm.contains("nes_FFFF:"));
        assert!(asm.contains("BANK(nes_FFFF)"));
    }

    #[test]
    fn dispatch_table_points_at_selected_block_bank() {
        let mut prg = vec![0xEA; 0x8000];
        prg[0] = 0x60;
        let graph = cfg::discover(0, &prg, &[0x8000]).unwrap();
        let asm = emit_cfg(&graph, EmitOptions { reset: 0x8000, max_blocks: Some(1) });
        assert!(asm.contains("SECTION \"NES dispatch table 0\", ROMX[$4000], BANK[32]"));
        assert!(asm.contains("db BANK(nes_8000), $00"));
        assert!(asm.contains("dw nes_8000"));
    }
}
