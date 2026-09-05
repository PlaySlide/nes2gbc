use std::fmt::Write;

use crate::ir::{Flag, IrOp, Operand, Register};

pub const NES_RAM_BASE: u16 = 0xC000;

fn label(addr: u16) -> String { format!("nes_{addr:04X}") }

fn mem_addr(op: Operand) -> Option<u16> {
    match op {
        Operand::ZeroPage(zp) => Some(NES_RAM_BASE + zp as u16),
        Operand::Absolute(addr) if addr < 0x0800 => Some(NES_RAM_BASE + addr),
        _ => None,
    }
}

fn reg_name(reg: Register) -> &'static str {
    match reg { Register::A => "a", Register::X => "b", Register::Y => "c", Register::Sp => "d" }
}

pub fn emit_ops(ops: &[IrOp]) -> String {
    let mut out = String::new();
    for op in ops {
        match *op {
            IrOp::SetFlag { flag: Flag::InterruptDisable, value: true } =>
                { writeln!(out, "    ; 6502 SEI (shadow interrupt state TODO)").unwrap(); }
            IrOp::SetFlag { flag: Flag::InterruptDisable, value: false } =>
                { writeln!(out, "    ; 6502 CLI (shadow interrupt state TODO)").unwrap(); }
            IrOp::SetFlag { flag: Flag::Decimal, .. } =>
                { writeln!(out, "    ; NES 2A03 ignores decimal arithmetic").unwrap(); }
            IrOp::SetFlag { flag: Flag::Carry, value: true } =>
                { writeln!(out, "    scf").unwrap(); }
            IrOp::SetFlag { flag: Flag::Carry, value: false } =>
                { writeln!(out, "    and a ; clear native carry").unwrap(); }
            IrOp::SetFlag { flag: Flag::Overflow, value: false } =>
                { writeln!(out, "    ; 6502 CLV shadow flag TODO").unwrap(); }
            IrOp::SetFlag { .. } =>
                { writeln!(out, "    ; shadow flag update TODO").unwrap(); }

            IrOp::Load { dst, src: Operand::Immediate(v) } =>
                { writeln!(out, "    ld {}, ${v:02X}", reg_name(dst)).unwrap(); }
            IrOp::Load { dst, src } => {
                if let Some(addr) = mem_addr(src) {
                    if dst == Register::A {
                        writeln!(out, "    ld a, [${addr:04X}]").unwrap();
                    } else {
                        writeln!(out, "    ld a, [${addr:04X}]").unwrap();
                        writeln!(out, "    ld {}, a", reg_name(dst)).unwrap();
                    }
                } else {
                    writeln!(out, "    ; unsupported memory load {src:?}").unwrap();
                }
            }

            IrOp::Store { src, dst } => {
                if let Some(addr) = mem_addr(dst) {
                    if src == Register::A {
                        writeln!(out, "    ld [${addr:04X}], a").unwrap();
                    } else {
                        writeln!(out, "    ld a, {}", reg_name(src)).unwrap();
                        writeln!(out, "    ld [${addr:04X}], a").unwrap();
                    }
                } else {
                    writeln!(out, "    ; unsupported memory store {dst:?}").unwrap();
                }
            }

            IrOp::Transfer { src, dst } => { writeln!(out, "    ld {}, {}", reg_name(dst), reg_name(src)).unwrap(); }
            IrOp::Dec(reg) => { writeln!(out, "    dec {}", reg_name(reg)).unwrap(); }

            IrOp::Compare { reg, rhs: Operand::Immediate(v) } => {
                if reg == Register::A {
                    writeln!(out, "    cp ${v:02X}").unwrap();
                } else {
                    writeln!(out, "    ld a, {}", reg_name(reg)).unwrap();
                    writeln!(out, "    cp ${v:02X}").unwrap();
                }
            }
            IrOp::Compare { reg, rhs } =>
                { writeln!(out, "    ; compare {reg:?} against {rhs:?} requires flag-shadow lowering").unwrap(); }

            IrOp::Branch { flag: Flag::Zero, when, target } =>
                { writeln!(out, "    jp {}, {}", if when { "z" } else { "nz" }, label(target)).unwrap(); }
            IrOp::Branch { flag: Flag::Carry, when, target } =>
                { writeln!(out, "    jp {}, {}", if when { "c" } else { "nc" }, label(target)).unwrap(); }
            IrOp::Branch { flag: Flag::Negative, when, target } => {
                writeln!(out, "    bit 7, a").unwrap();
                writeln!(out, "    jp {}, {}", if when { "nz" } else { "z" }, label(target)).unwrap();
            }
            IrOp::Branch { flag: Flag::Overflow, .. } =>
                { writeln!(out, "    ; 6502 overflow branch requires shadow V flag").unwrap(); }
            IrOp::Branch { flag, when, target } =>
                { writeln!(out, "    ; branch {flag:?}={when} -> {}", label(target)).unwrap(); }

            IrOp::Jump(target) => { writeln!(out, "    jp {}", label(target)).unwrap(); }
            IrOp::Call(target) => { writeln!(out, "    call {}", label(target)).unwrap(); }
            IrOp::Return => { writeln!(out, "    ret").unwrap(); }

            IrOp::ReadIo { addr, dst } => {
                writeln!(out, "    call nes_io_read_{addr:04X}").unwrap();
                if dst != Register::A { writeln!(out, "    ld {}, a", reg_name(dst)).unwrap(); }
            }
            IrOp::WriteIo { addr, src } => {
                if src != Register::A { writeln!(out, "    ld a, {}", reg_name(src)).unwrap(); }
                writeln!(out, "    call nes_io_write_{addr:04X}").unwrap();
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn direct_zero_page_maps_to_wram() {
        let asm = emit_ops(&[
            IrOp::Load { dst: Register::A, src: Operand::ZeroPage(0x10) },
            IrOp::Store { src: Register::A, dst: Operand::ZeroPage(0x11) },
        ]);
        assert!(asm.contains("ld a, [$C010]"));
        assert!(asm.contains("ld [$C011], a"));
    }
}
