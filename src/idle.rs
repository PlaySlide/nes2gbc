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

/// A block that is a lone `JMP self`: it can only be left by an interrupt. When
/// such a spin is reachable from the NMI handler, the handler "ends" by waiting
/// for the next NMI to interrupt it (Slalom $A697) - there is never an RTI.
pub fn is_forever_spin(block: &BasicBlock) -> bool {
    matches!(block.instructions.as_slice(), [i] if i.def.mnemonic == N::Jmp && i.def.mode == M::Absolute && i.operand == block.start)
}

pub fn forever_spin_heads(graph: &ControlFlowGraph) -> Vec<u16> {
    graph.blocks.values().filter(|b| is_forever_spin(b)).map(|b| b.start).collect()
}

pub fn idle_spin_heads(graph: &ControlFlowGraph) -> Vec<u16> {
    graph.blocks.values().filter(|b| is_idle_spin(b)).map(|b| b.start).collect()
}

/// Pure delay loop: a block that is exactly `DEX; BNE self` (or `DEY; BNE self`).
/// It always exits with the register 0, Z=1, N=0 and nothing else changed, so
/// the DEX/DEY can be replaced by `LDX/LDY #0` (identical end state, BNE falls
/// through) - only the burned cycles disappear. Opt-out NES2GBC_NO_DELAY_ELIDE=1.
pub fn elide_delay_loops(graph: &mut ControlFlowGraph) -> usize {
    if std::env::var_os("NES2GBC_NO_DELAY_ELIDE").is_some() {
        return 0;
    }
    // Only nested delays (inner `DEX; BNE S` followed by `DEY; BNE S`, or X/Y
    // swapped): short single-level loops often time raster splits after a
    // sprite-0 wait and must keep their cost.
    let dec = |b: &BasicBlock| -> Option<N> {
        match b.instructions.as_slice() {
            [d, br] if br.def.mnemonic == N::Bne && d.def.mode == M::Implied && matches!(d.def.mnemonic, N::Dex | N::Dey) => {
                Some(d.def.mnemonic)
            }
            _ => None,
        }
    };
    let taken = |b: &BasicBlock| b.edges.iter().find(|e| e.kind == EdgeKind::BranchTaken).and_then(|e| e.target);
    let fall = |b: &BasicBlock| b.edges.iter().find(|e| e.kind == EdgeKind::Fallthrough).and_then(|e| e.target);
    let mut todo = Vec::new();
    for b in graph.blocks.values() {
        let Some(inner) = dec(b) else { continue };
        if taken(b) != Some(b.start) { continue; }
        let Some(outer) = fall(b).and_then(|f| graph.blocks.get(&f)) else { continue };
        if dec(outer).is_some_and(|o| o != inner) && taken(outer) == Some(b.start) {
            todo.push(b.start);
        }
    }
    for pc in &todo {
        let d = &mut graph.blocks.get_mut(pc).unwrap().instructions[0];
        let (op, m) = if d.def.mnemonic == N::Dex { (0xA2, N::Ldx) } else { (0xA0, N::Ldy) };
        d.opcode = op;
        d.def.mnemonic = m;
        d.def.mode = M::Immediate;
        d.operand = 0;
    }
    todo.len()
}

/// 16-bit RAM delay counter: a self-loop block that is exactly
/// `LDA m; CLC; ADC #1; STA m; LDA m+1; ADC #0; STA m+1; ORA m; BNE self`.
/// Whatever the start value it exits with m = m+1 = 0, A = 0, Z=1, N=0, C=1,
/// V=0 and nothing else changed, so it is rewritten in place to
/// `LDA #0; SEC; CLV; STA m; NOP; NOP; STA m+1; ORA m; BNE` (falls through on
/// the first pass). Only the burned cycles disappear (Bases Loaded $8093:
/// 4096 iterations after START). Opt-out NES2GBC_DELAY16_ELIDE=0.
pub fn elide_delay16_loops(graph: &mut ControlFlowGraph) -> usize {
    if std::env::var("NES2GBC_DELAY16_ELIDE").is_ok_and(|v| v == "0") {
        return 0;
    }
    let ram = |a: u16| a < 0x0800 || (0x6000..0x8000).contains(&a);
    let mem = |i: &DecodedInstruction, n: N| i.def.mnemonic == n && matches!(i.def.mode, M::ZeroPage | M::Absolute) && ram(i.operand);
    let imm = |i: &DecodedInstruction, n: N, k: u16| i.def.mnemonic == n && i.def.mode == M::Immediate && i.operand & 0xFF == k;
    let mut todo = Vec::new();
    for b in graph.blocks.values() {
        let selfb = b.edges.iter().any(|e| e.kind == EdgeKind::BranchTaken && e.target == Some(b.start));
        if !selfb || b.instructions.first().map(|i| i.pc) != Some(b.start) {
            continue;
        }
        if let [l0, c, a1, s0, l1, a0, s1, o, br] = b.instructions.as_slice() {
            let m = l0.operand;
            if mem(l0, N::Lda) && c.def.mnemonic == N::Clc && imm(a1, N::Adc, 1) && mem(s0, N::Sta) && s0.operand == m
                && mem(l1, N::Lda) && l1.operand == m.wrapping_add(1) && imm(a0, N::Adc, 0)
                && mem(s1, N::Sta) && s1.operand == l1.operand && mem(o, N::Ora) && o.operand == m && br.def.mnemonic == N::Bne
            {
                todo.push(b.start);
            }
        }
    }
    for pc in &todo {
        let ins = &mut graph.blocks.get_mut(pc).unwrap().instructions;
        let set = |i: &mut DecodedInstruction, op: u8, n: N, mode: M, v: u16| {
            i.opcode = op;
            i.def.mnemonic = n;
            i.def.mode = mode;
            i.operand = v;
        };
        set(&mut ins[0], 0xA9, N::Lda, M::Immediate, 0);
        set(&mut ins[1], 0x38, N::Sec, M::Implied, 0);
        set(&mut ins[2], 0xB8, N::Clv, M::Implied, 0);
        set(&mut ins[4], 0xEA, N::Nop, M::Implied, 0);
        set(&mut ins[5], 0xEA, N::Nop, M::Implied, 0);
    }
    todo.len()
}

/// Register-only counting self-loops that the NES2GBC_FAST_LOOPS post-pass
/// (tools/fast_loops.py) fast-forwards: `DEX|DEY; BNE self`, `CLC; ADC #n;
/// BCC self`, `SEC; SBC #n; BCS self`. The post-pass runs every iteration but
/// the last in a tight host loop; the original block then does the final one,
/// so the exit state is exact. Returns (head, kind, imm), kind in x/y/adc/sbc.
pub fn fast_loop_heads(graph: &ControlFlowGraph) -> Vec<(u16, &'static str, u8)> {
    let mut out = Vec::new();
    for b in graph.blocks.values() {
        let selfb = b.edges.iter().any(|e| e.kind == EdgeKind::BranchTaken && e.target == Some(b.start));
        if !selfb {
            continue;
        }
        match b.instructions.as_slice() {
            [d, br] if br.def.mnemonic == N::Bne && d.def.mode == M::Implied => match d.def.mnemonic {
                N::Dex => out.push((b.start, "x", 0)),
                N::Dey => out.push((b.start, "y", 0)),
                _ => {}
            },
            [f, op, br] if op.def.mode == M::Immediate && op.operand & 0xFF != 0 => {
                let k = op.operand as u8;
                match (f.def.mnemonic, op.def.mnemonic, br.def.mnemonic) {
                    (N::Clc, N::Adc, N::Bcc) => out.push((b.start, "adc", k)),
                    (N::Sec, N::Sbc, N::Bcs) => out.push((b.start, "sbc", k)),
                    _ => {}
                }
            }
            _ => {}
        }
    }
    out
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
