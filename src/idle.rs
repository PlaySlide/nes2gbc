//! Idle-spin detection for NES2GBC_HALT_IDLE (tools/halt_idle_loops.py).
//!
//! A spin is a single 6502 basic block that branches or jumps back to its own
//! start and only reads NES work RAM / PRG RAM: loads, compares, BIT, AND/ORA,
//! NOP and branches. With memory unchanged every further iteration is a no-op
//! (each op is idempotent for fixed memory), so only an interrupt (the
//! translated NMI) can make the loop exit. The runtime may therefore HALT the
//! host CPU on the loop's back-edge, or start the next NMI right there, without
//! changing any 6502-visible behavior other than timing.
use crate::cfg::{BasicBlock, ControlFlowGraph, EdgeKind};
use crate::cpu6502::{AddressingMode as M, DecodedInstruction, Mnemonic as N};

/// Read operand is NES internal RAM ($0000-$07FF) or PRG RAM ($6000-$7FFF):
/// memory only the CPU itself can change (no I/O side effects or hardware state).
fn ram_read(ins: &DecodedInstruction) -> bool {
    let ram = |lo: u32, hi: u32| (lo < 0x0800 && hi < 0x0800) || ((0x6000..0x8000).contains(&lo) && (0x6000..0x8000).contains(&hi));
    let a = ins.operand as u32;
    match ins.def.mode {
        M::Immediate | M::ZeroPage | M::ZeroPageX | M::ZeroPageY => true,
        M::Absolute => ram(a, a),
        M::AbsoluteX | M::AbsoluteY => ram(a, a + 0xFF),
        _ => false,
    }
}

fn pure_spin_op(ins: &DecodedInstruction) -> bool {
    match ins.def.mnemonic {
        N::Lda | N::Ldx | N::Ldy | N::Cmp | N::Cpx | N::Cpy | N::And | N::Ora | N::Bit => ram_read(ins),
        N::Beq | N::Bne | N::Bpl | N::Bmi | N::Bcc | N::Bcs | N::Bvc | N::Bvs => true,
        N::Jmp => ins.def.mode == M::Absolute,
        N::Nop => ins.def.mode == M::Implied,
        _ => false,
    }
}

/// True when `block` is a provable idle spin (see module docs).
pub fn is_idle_spin(block: &BasicBlock) -> bool {
    !block.instructions.is_empty()
        && block.instructions.iter().all(pure_spin_op)
        && block.edges.iter().any(|e| {
            matches!(e.kind, EdgeKind::BranchTaken | EdgeKind::Jump) && e.target == Some(block.start)
        })
        && block.edges.iter().all(|e| {
            matches!(e.kind, EdgeKind::BranchTaken | EdgeKind::Jump | EdgeKind::Fallthrough)
        })
}

pub fn idle_spin_heads(graph: &ControlFlowGraph) -> Vec<u16> {
    graph.blocks.values().filter(|b| is_idle_spin(b)).map(|b| b.start).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cfg::Edge;
    use crate::cpu6502::InstructionDef;

    fn ins(pc: u16, mnemonic: N, mode: M, operand: u16) -> DecodedInstruction {
        DecodedInstruction { pc, opcode: 0, def: InstructionDef { mnemonic, mode }, operand }
    }
    fn block(start: u16, instructions: Vec<DecodedInstruction>, edges: Vec<(EdgeKind, u16)>) -> BasicBlock {
        BasicBlock { start, instructions, edges: edges.into_iter().map(|(kind, t)| Edge { kind, target: Some(t) }).collect() }
    }

    #[test]
    fn ram_flag_wait_and_jmp_self_are_spins() {
        let wait = block(0xC02B, vec![ins(0xC02B, N::Lda, M::ZeroPage, 0x1F), ins(0xC02D, N::Beq, M::Relative, 0xC02B)],
            vec![(EdgeKind::BranchTaken, 0xC02B), (EdgeKind::Fallthrough, 0xC02F)]);
        assert!(is_idle_spin(&wait));
        let jmp = block(0x8057, vec![ins(0x8057, N::Jmp, M::Absolute, 0x8057)], vec![(EdgeKind::Jump, 0x8057)]);
        assert!(is_idle_spin(&jmp));
    }

    #[test]
    fn io_reads_writes_counters_and_non_self_loops_are_not_spins() {
        let ppu = block(0xC000, vec![ins(0xC000, N::Lda, M::Absolute, 0x2002), ins(0xC003, N::Bpl, M::Relative, 0xC000)],
            vec![(EdgeKind::BranchTaken, 0xC000), (EdgeKind::Fallthrough, 0xC005)]);
        assert!(!is_idle_spin(&ppu));
        let counted = block(0xC000, vec![ins(0xC000, N::Dex, M::Implied, 0), ins(0xC001, N::Bne, M::Relative, 0xC000)],
            vec![(EdgeKind::BranchTaken, 0xC000), (EdgeKind::Fallthrough, 0xC003)]);
        assert!(!is_idle_spin(&counted));
        let store = block(0xC000, vec![ins(0xC000, N::Inc, M::ZeroPage, 0x34), ins(0xC002, N::Jmp, M::Absolute, 0xC000)],
            vec![(EdgeKind::Jump, 0xC000)]);
        assert!(!is_idle_spin(&store));
        let other = block(0xC000, vec![ins(0xC000, N::Lda, M::ZeroPage, 0x10), ins(0xC002, N::Beq, M::Relative, 0xC100)],
            vec![(EdgeKind::BranchTaken, 0xC100), (EdgeKind::Fallthrough, 0xC004)]);
        assert!(!is_idle_spin(&other));
        let wide = block(0xC000, vec![ins(0xC000, N::Lda, M::AbsoluteX, 0x0780), ins(0xC003, N::Beq, M::Relative, 0xC000)],
            vec![(EdgeKind::BranchTaken, 0xC000), (EdgeKind::Fallthrough, 0xC005)]);
        assert!(!is_idle_spin(&wide));
    }
}
