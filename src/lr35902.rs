use std::fmt::Write;

use crate::ir::{Flag, IrOp, LogicOp, Operand, Register, StackValue};

pub const NES_RAM_BASE: u16 = 0xC000;

fn label(addr: u16) -> String { format!("nes_{addr:04X}") }

fn state_label(reg: Register) -> &'static str {
    match reg {
        Register::A => "nes_a",
        Register::X => "nes_x",
        Register::Y => "nes_y",
        Register::Sp => "nes_sp",
    }
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

fn direct_ram_addr(addr: u16) -> Option<u16> {
    if addr < 0x2000 {
        Some(NES_RAM_BASE + (addr & 0x07FF))
    } else {
        None
    }
}

fn emit_effective_addr(out: &mut String, op: Operand) -> bool {
    match op {
        Operand::ZeroPage(zp) => {
            writeln!(out, "    ld h, $C0").unwrap();
            writeln!(out, "    ld l, ${zp:02X}").unwrap();
            true
        }
        Operand::ZeroPageX(zp) => {
            writeln!(out, "    ld a, [nes_x]").unwrap();
            writeln!(out, "    add ${zp:02X}").unwrap();
            writeln!(out, "    ld l, a").unwrap();
            writeln!(out, "    ld h, $C0").unwrap();
            true
        }
        Operand::ZeroPageY(zp) => {
            writeln!(out, "    ld a, [nes_y]").unwrap();
            writeln!(out, "    add ${zp:02X}").unwrap();
            writeln!(out, "    ld l, a").unwrap();
            writeln!(out, "    ld h, $C0").unwrap();
            true
        }
        Operand::Absolute(addr) => {
            if let Some(mapped) = direct_ram_addr(addr) {
                writeln!(out, "    ld hl, ${mapped:04X}").unwrap();
                true
            } else {
                false
            }
        }
        Operand::AbsoluteX(addr) => {
            if addr < 0x0800 && addr + 0x00FF < 0x0800 {
                let mapped = NES_RAM_BASE + addr;
                writeln!(out, "    ld hl, ${mapped:04X}").unwrap();
                writeln!(out, "    ld a, [nes_x]").unwrap();
                writeln!(out, "    add l").unwrap();
                writeln!(out, "    ld l, a").unwrap();
                writeln!(out, "    jr nc, .no_carry\@").unwrap();
                writeln!(out, "    inc h").unwrap();
                writeln!(out, ".no_carry\@:").unwrap();
                true
            } else {
                false
            }
        }
        Operand::AbsoluteY(addr) => {
            if addr < 0x0800 && addr + 0x00FF < 0x0800 {
                let mapped = NES_RAM_BASE + addr;
                writeln!(out, "    ld hl, ${mapped:04X}").unwrap();
                writeln!(out, "    ld a, [nes_y]").unwrap();
                writeln!(out, "    add l").unwrap();
                writeln!(out, "    ld l, a").unwrap();
                writeln!(out, "    jr nc, .no_carry\@").unwrap();
                writeln!(out, "    inc h").unwrap();
                writeln!(out, ".no_carry\@:").unwrap();
                true
            } else {
                false
            }
        }
        Operand::IndexedIndirect(zp) => {
            writeln!(out, "    ld a, [nes_x]").unwrap();
            writeln!(out, "    add ${zp:02X}").unwrap();
            writeln!(out, "    ld e, a").unwrap();
            writeln!(out, "    ld d, $C0").unwrap();
            writeln!(out, "    ld a, [de]").unwrap();
            writeln!(out, "    ld l, a").unwrap();
            writeln!(out, "    inc e").unwrap();
            writeln!(out, "    ld a, [de]").unwrap();
            writeln!(out, "    ld h, a").unwrap();
            writeln!(out, "    call nes_map_cpu_addr_hl").unwrap();
            true
        }
        Operand::IndirectIndexed(zp) => {
            writeln!(out, "    ld de, $C0{zp:02X}").unwrap();
            writeln!(out, "    ld a, [de]").unwrap();
            writeln!(out, "    ld l, a").unwrap();
            writeln!(out, "    inc e").unwrap();
            writeln!(out, "    ld a, [de]").unwrap();
            writeln!(out, "    ld h, a").unwrap();
            writeln!(out, "    ld a, [nes_y]").unwrap();
            writeln!(out, "    add l").unwrap();
            writeln!(out, "    ld l, a").unwrap();
            writeln!(out, "    jr nc, .iy_no_carry\@").unwrap();
            writeln!(out, "    inc h").unwrap();
            writeln!(out, ".iy_no_carry\@:").unwrap();
            writeln!(out, "    call nes_map_cpu_addr_hl").unwrap();
            true
        }
        Operand::Immediate(_) => false,
    }
}

fn emit_load_operand_to_a(out: &mut String, src: Operand) {
    match src {
        Operand::Immediate(v) => { writeln!(out, "    ld a, ${v:02X}").unwrap(); }
        _ => {
            if emit_effective_addr(out, src) {
                writeln!(out, "    ld a, [hl]").unwrap();
            } else {
                match src {
                    Operand::Absolute(addr) => {
                        writeln!(out, "    ld hl, ${addr:04X}").unwrap();
                        writeln!(out, "    call nes_cpu_read").unwrap();
                    }
                    Operand::AbsoluteX(addr) => {
                        writeln!(out, "    ld hl, ${addr:04X}").unwrap();
                        writeln!(out, "    ld a, [nes_x]").unwrap();
                        writeln!(out, "    call nes_add_a_to_hl").unwrap();
                        writeln!(out, "    call nes_cpu_read").unwrap();
                    }
                    Operand::AbsoluteY(addr) => {
                        writeln!(out, "    ld hl, ${addr:04X}").unwrap();
                        writeln!(out, "    ld a, [nes_y]").unwrap();
                        writeln!(out, "    call nes_add_a_to_hl").unwrap();
                        writeln!(out, "    call nes_cpu_read").unwrap();
                    }
                    _ => writeln!(out, "    call nes_unimplemented_operand_read").unwrap(),
                }
            }
        }
    }
}

fn emit_store_a_to_operand(out: &mut String, dst: Operand) {
    if emit_effective_addr(out, dst) {
        writeln!(out, "    ld [hl], a").unwrap();
    } else {
        match dst {
            Operand::Absolute(addr) => {
                writeln!(out, "    ld hl, ${addr:04X}").unwrap();
                writeln!(out, "    call nes_cpu_write").unwrap();
            }
            Operand::AbsoluteX(addr) => {
                writeln!(out, "    push af").unwrap();
                writeln!(out, "    ld hl, ${addr:04X}").unwrap();
                writeln!(out, "    ld a, [nes_x]").unwrap();
                writeln!(out, "    call nes_add_a_to_hl").unwrap();
                writeln!(out, "    pop af").unwrap();
                writeln!(out, "    call nes_cpu_write").unwrap();
            }
            Operand::AbsoluteY(addr) => {
                writeln!(out, "    push af").unwrap();
                writeln!(out, "    ld hl, ${addr:04X}").unwrap();
                writeln!(out, "    ld a, [nes_y]").unwrap();
                writeln!(out, "    call nes_add_a_to_hl").unwrap();
                writeln!(out, "    pop af").unwrap();
                writeln!(out, "    call nes_cpu_write").unwrap();
            }
            _ => writeln!(out, "    call nes_unimplemented_operand_write").unwrap(),
        }
    }
}

fn emit_update_nz(out: &mut String) {
    writeln!(out, "    call nes_set_nz_from_a").unwrap();
}

pub fn emit_ops(ops: &[IrOp]) -> String {
    let mut out = String::new();

    for op in ops {
        match *op {
            IrOp::SetFlag { flag, value } => {
                writeln!(out, "    ld a, [nes_p]").unwrap();
                if value {
                    writeln!(out, "    or ${:02X}", flag_mask(flag)).unwrap();
                } else {
                    writeln!(out, "    and ${:02X}", !flag_mask(flag)).unwrap();
                }
                writeln!(out, "    ld [nes_p], a").unwrap();
            }

            IrOp::Load { dst, src } => {
                emit_load_operand_to_a(&mut out, src);
                writeln!(out, "    ld [{}], a", state_label(dst)).unwrap();
                emit_update_nz(&mut out);
            }

            IrOp::Store { src, dst } => {
                writeln!(out, "    ld a, [{}]", state_label(src)).unwrap();
                emit_store_a_to_operand(&mut out, dst);
            }

            IrOp::Transfer { src, dst, update_nz } => {
                writeln!(out, "    ld a, [{}]", state_label(src)).unwrap();
                writeln!(out, "    ld [{}], a", state_label(dst)).unwrap();
                if update_nz { emit_update_nz(&mut out); }
            }

            IrOp::Inc(reg) => {
                writeln!(out, "    ld a, [{}]", state_label(reg)).unwrap();
                writeln!(out, "    inc a").unwrap();
                writeln!(out, "    ld [{}], a", state_label(reg)).unwrap();
                emit_update_nz(&mut out);
            }

            IrOp::Dec(reg) => {
                writeln!(out, "    ld a, [{}]", state_label(reg)).unwrap();
                writeln!(out, "    dec a").unwrap();
                writeln!(out, "    ld [{}], a", state_label(reg)).unwrap();
                emit_update_nz(&mut out);
            }

            IrOp::Logic { op, rhs } => {
                emit_load_operand_to_a(&mut out, rhs);
                writeln!(out, "    ld e, a").unwrap();
                writeln!(out, "    ld a, [nes_a]").unwrap();
                match op {
                    LogicOp::And => writeln!(out, "    and e").unwrap(),
                    LogicOp::Ora => writeln!(out, "    or e").unwrap(),
                    LogicOp::Eor => writeln!(out, "    xor e").unwrap(),
                }
                writeln!(out, "    ld [nes_a], a").unwrap();
                emit_update_nz(&mut out);
            }

            IrOp::Compare { reg, rhs } => {
                emit_load_operand_to_a(&mut out, rhs);
                writeln!(out, "    ld e, a").unwrap();
                writeln!(out, "    ld a, [{}]", state_label(reg)).unwrap();
                writeln!(out, "    call nes_compare_a_e").unwrap();
            }

            IrOp::StackPush(StackValue::A) => {
                writeln!(out, "    ld a, [nes_a]").unwrap();
                writeln!(out, "    call nes_stack_push_a").unwrap();
            }
            IrOp::StackPush(StackValue::Status) => {
                writeln!(out, "    ld a, [nes_p]").unwrap();
                writeln!(out, "    or $30").unwrap();
                writeln!(out, "    call nes_stack_push_a").unwrap();
            }
            IrOp::StackPop(StackValue::A) => {
                writeln!(out, "    call nes_stack_pop_a").unwrap();
                writeln!(out, "    ld [nes_a], a").unwrap();
                emit_update_nz(&mut out);
            }
            IrOp::StackPop(StackValue::Status) => {
                writeln!(out, "    call nes_stack_pop_a").unwrap();
                writeln!(out, "    or $20").unwrap();
                writeln!(out, "    and $EF").unwrap();
                writeln!(out, "    ld [nes_p], a").unwrap();
            }

            IrOp::Branch { flag, when, target } => {
                writeln!(out, "    ld a, [nes_p]").unwrap();
                writeln!(out, "    and ${:02X}", flag_mask(flag)).unwrap();
                writeln!(out, "    jp {}, {}", if when { "nz" } else { "z" }, label(target)).unwrap();
            }

            IrOp::Jump(target) => { writeln!(out, "    jp {}", label(target)).unwrap(); }

            IrOp::Call { target, return_addr } => {
                writeln!(out, "    ld hl, ${return_addr:04X}").unwrap();
                writeln!(out, "    call nes_stack_push_return_hl").unwrap();
                writeln!(out, "    jp {}", label(target)).unwrap();
            }

            IrOp::Return => {
                writeln!(out, "    call nes_stack_pop_return_hl").unwrap();
                writeln!(out, "    inc hl").unwrap();
                writeln!(out, "    jp nes_dispatch_hl").unwrap();
            }

            IrOp::ReadIo { addr, dst } => {
                writeln!(out, "    ld hl, ${addr:04X}").unwrap();
                writeln!(out, "    call nes_cpu_read").unwrap();
                writeln!(out, "    ld [{}], a", state_label(dst)).unwrap();
                emit_update_nz(&mut out);
            }

            IrOp::WriteIo { addr, src } => {
                writeln!(out, "    ld hl, ${addr:04X}").unwrap();
                writeln!(out, "    ld a, [{}]", state_label(src)).unwrap();
                writeln!(out, "    call nes_cpu_write").unwrap();
            }

            IrOp::Nop => {}
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
        assert!(asm.contains("ld h, $C0"));
        assert!(asm.contains("ld l, $10"));
        assert!(asm.contains("ld [hl], a"));
    }

    #[test]
    fn branches_read_shadow_status() {
        let asm = emit_ops(&[IrOp::Branch { flag: Flag::Negative, when: false, target: 0x9000 }]);
        assert!(asm.contains("ld a, [nes_p]"));
        assert!(asm.contains("and $80"));
        assert!(asm.contains("jp z, nes_9000"));
    }
}
