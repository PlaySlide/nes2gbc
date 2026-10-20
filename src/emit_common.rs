//! Emission helpers shared by the plain (recompile.rs) and superblock
//! (state_superblock.rs) translators. Stage 1 of docs/IR_REWRITE.md moves
//! duplicated logic here; behaviour and output are unchanged.
use std::fmt::Write;

use crate::mir::MirBuf;

use crate::ir::Flag;

pub(crate) fn estimated_block_size(block: &crate::cfg::BasicBlock) -> usize {
    64 + block.instructions.len() * 96
}

pub(crate) fn emit_pc_dispatch(out: &mut MirBuf, target: u16) {
    writeln!(out, "    ld hl, ${target:04X}").unwrap();
    writeln!(out, "    jp nes_dispatch_hl").unwrap();
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

