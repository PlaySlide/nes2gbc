use std::fmt::Write;

use crate::ir::{ArithmeticOp, Flag, IrOp, LogicOp, ModifyOp, ModifyTarget, Operand, Register, StackValue};

pub const NES_RAM_BASE: u16 = 0xC000;

fn state_label(reg: Register) -> &'static str {
    match reg {
        Register::A => "nes_a",
        Register::X => "nes_x",
        Register::Y => "nes_y",
        Register::Sp => "nes_sp",
    }
}

fn emit_reg_to_a(out: &mut String, reg: Register) {
    match reg {
        Register::A => writeln!(out, "    ld a, b").unwrap(),
        Register::X | Register::Y | Register::Sp => {
            writeln!(out, "    ld a, [{}]", state_label(reg)).unwrap();
        }
    }
}

fn emit_a_to_reg(out: &mut String, reg: Register) {
    match reg {
        Register::A => writeln!(out, "    ld b, a").unwrap(),
        Register::X | Register::Y | Register::Sp => {
            writeln!(out, "    ld [{}], a", state_label(reg)).unwrap();
        }
    }
}

fn emit_spill_a(out: &mut String) {
    writeln!(out, "    ld a, b").unwrap();
    writeln!(out, "    ld [nes_a], a").unwrap();
}

fn emit_preserved_b_call(out: &mut String, target: &str) {
    writeln!(out, "    push bc").unwrap();
    writeln!(out, "    call {target}").unwrap();
    writeln!(out, "    pop bc").unwrap();
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
                writeln!(out, "    call nes_add_a_to_hl").unwrap();
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
                writeln!(out, "    call nes_add_a_to_hl").unwrap();
                true
            } else {
                false
            }
        }
        Operand::IndexedIndirect(_) | Operand::IndirectIndexed(_) => false,
        Operand::Immediate(_) => false,
    }
}

fn emit_indirect_addr(out: &mut String, src: Operand) {
    match src {
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
        }
        Operand::IndirectIndexed(zp) => {
            writeln!(out, "    ld e, ${zp:02X}").unwrap();
            writeln!(out, "    ld d, $C0").unwrap();
            writeln!(out, "    ld a, [de]").unwrap();
            writeln!(out, "    ld l, a").unwrap();
            writeln!(out, "    inc e").unwrap();
            writeln!(out, "    ld a, [de]").unwrap();
            writeln!(out, "    ld h, a").unwrap();
            writeln!(out, "    ld a, [nes_y]").unwrap();
            writeln!(out, "    call nes_add_a_to_hl").unwrap();
        }
        _ => unreachable!(),
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
                        emit_preserved_b_call(out, "nes_cpu_read");
                    }
                    Operand::AbsoluteX(addr) => {
                        writeln!(out, "    ld hl, ${addr:04X}").unwrap();
                        writeln!(out, "    ld a, [nes_x]").unwrap();
                        writeln!(out, "    call nes_add_a_to_hl").unwrap();
                        emit_preserved_b_call(out, "nes_cpu_read");
                    }
                    Operand::AbsoluteY(addr) => {
                        writeln!(out, "    ld hl, ${addr:04X}").unwrap();
                        writeln!(out, "    ld a, [nes_y]").unwrap();
                        writeln!(out, "    call nes_add_a_to_hl").unwrap();
                        emit_preserved_b_call(out, "nes_cpu_read");
                    }
                    Operand::IndexedIndirect(_) | Operand::IndirectIndexed(_) => {
                        emit_indirect_addr(out, src);
                        emit_preserved_b_call(out, "nes_cpu_read");
                    }
                    _ => writeln!(out, "    call nes_unimplemented_operand_read").unwrap(),
                }
            }
        }
    }
}

fn emit_store_a_to_operand(out: &mut String, dst: Operand) {
    writeln!(out, "    push af").unwrap();

    if emit_effective_addr(out, dst) {
        writeln!(out, "    pop af").unwrap();
        writeln!(out, "    ld [hl], a").unwrap();
        return;
    }

    match dst {
        Operand::Absolute(addr) => {
            writeln!(out, "    ld hl, ${addr:04X}").unwrap();
        }
        Operand::AbsoluteX(addr) => {
            writeln!(out, "    ld hl, ${addr:04X}").unwrap();
            writeln!(out, "    ld a, [nes_x]").unwrap();
            writeln!(out, "    call nes_add_a_to_hl").unwrap();
        }
        Operand::AbsoluteY(addr) => {
            writeln!(out, "    ld hl, ${addr:04X}").unwrap();
            writeln!(out, "    ld a, [nes_y]").unwrap();
            writeln!(out, "    call nes_add_a_to_hl").unwrap();
        }
        Operand::IndexedIndirect(_) | Operand::IndirectIndexed(_) => {
            emit_indirect_addr(out, dst);
        }
        _ => {
            writeln!(out, "    pop af").unwrap();
            writeln!(out, "    call nes_unimplemented_operand_write").unwrap();
            return;
        }
    }

    writeln!(out, "    pop af").unwrap();
    emit_preserved_b_call(out, "nes_cpu_write");
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
                emit_a_to_reg(&mut out, dst);
                emit_update_nz(&mut out);
            }

            IrOp::Store { src, dst } => {
                emit_reg_to_a(&mut out, src);
                emit_store_a_to_operand(&mut out, dst);
            }

            IrOp::Transfer { src, dst, update_nz } => {
                emit_reg_to_a(&mut out, src);
                emit_a_to_reg(&mut out, dst);
                if update_nz { emit_update_nz(&mut out); }
            }

            IrOp::Inc(reg) => {
                emit_reg_to_a(&mut out, reg);
                writeln!(out, "    inc a").unwrap();
                emit_a_to_reg(&mut out, reg);
                emit_update_nz(&mut out);
            }

            IrOp::Dec(reg) => {
                emit_reg_to_a(&mut out, reg);
                writeln!(out, "    dec a").unwrap();
                emit_a_to_reg(&mut out, reg);
                emit_update_nz(&mut out);
            }

            IrOp::Logic { op, rhs } => {
                emit_load_operand_to_a(&mut out, rhs);
                writeln!(out, "    ld e, a").unwrap();
                writeln!(out, "    ld a, b").unwrap();
                match op {
                    LogicOp::And => writeln!(out, "    and e").unwrap(),
                    LogicOp::Ora => writeln!(out, "    or e").unwrap(),
                    LogicOp::Eor => writeln!(out, "    xor e").unwrap(),
                }
                writeln!(out, "    ld b, a").unwrap();
                emit_update_nz(&mut out);
            }

            IrOp::Arithmetic { op, rhs } => {
                emit_load_operand_to_a(&mut out, rhs);
                writeln!(out, "    ld e, a").unwrap();
                writeln!(out, "    ld a, b").unwrap();
                match op {
                    ArithmeticOp::Adc => emit_preserved_b_call(&mut out, "nes_adc_a_e"),
                    ArithmeticOp::Sbc => emit_preserved_b_call(&mut out, "nes_sbc_a_e"),
                }
                writeln!(out, "    ld b, a").unwrap();
            }

            IrOp::Modify { op, target } => {
                let memory_target = match target {
                    ModifyTarget::Accumulator => {
                        writeln!(out, "    ld a, b").unwrap();
                        None
                    }
                    ModifyTarget::Memory(mem) => {
                        emit_load_operand_to_a(&mut out, mem);
                        Some(mem)
                    }
                };

                match op {
                    ModifyOp::Inc => {
                        writeln!(out, "    inc a").unwrap();
                        emit_update_nz(&mut out);
                    }
                    ModifyOp::Dec => {
                        writeln!(out, "    dec a").unwrap();
                        emit_update_nz(&mut out);
                    }
                    ModifyOp::Asl => { writeln!(out, "    call nes_asl_a").unwrap(); }
                    ModifyOp::Lsr => { writeln!(out, "    call nes_lsr_a").unwrap(); }
                    ModifyOp::Rol => { writeln!(out, "    call nes_rol_a").unwrap(); }
                    ModifyOp::Ror => { writeln!(out, "    call nes_ror_a").unwrap(); }
                }

                if let Some(mem) = memory_target {
                    emit_store_a_to_operand(&mut out, mem);
                } else {
                    writeln!(out, "    ld b, a").unwrap();
                }
            }

            IrOp::Bit { rhs } => {
                emit_load_operand_to_a(&mut out, rhs);
                writeln!(out, "    ld e, a").unwrap();
                writeln!(out, "    ld a, b").unwrap();
                emit_preserved_b_call(&mut out, "nes_bit_a_e");
            }
            IrOp::Compare { reg, rhs } => {
                emit_load_operand_to_a(&mut out, rhs);
                writeln!(out, "    ld e, a").unwrap();
                emit_reg_to_a(&mut out, reg);
                emit_preserved_b_call(&mut out, "nes_compare_a_e");
            }

            IrOp::StackPush(StackValue::A) => {
                writeln!(out, "    ld a, b").unwrap();
                writeln!(out, "    call nes_stack_push_a").unwrap();
            }
            IrOp::StackPush(StackValue::Status) => {
                writeln!(out, "    ld a, [nes_p]").unwrap();
                writeln!(out, "    or $30").unwrap();
                writeln!(out, "    call nes_stack_push_a").unwrap();
            }
            IrOp::StackPop(StackValue::A) => {
                writeln!(out, "    call nes_stack_pop_a").unwrap();
                writeln!(out, "    ld b, a").unwrap();
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
                writeln!(out, "    jr {}, :+", if when { "z" } else { "nz" }).unwrap();
                emit_spill_a(&mut out);
                writeln!(out, "    ld hl, ${target:04X}").unwrap();
                writeln!(out, "    jp nes_dispatch_hl").unwrap();
                writeln!(out, ":").unwrap();
            }

            IrOp::Jump(target) => {
                emit_spill_a(&mut out);
                writeln!(out, "    ld hl, ${target:04X}").unwrap();
                writeln!(out, "    jp nes_dispatch_hl").unwrap();
            }

            IrOp::JumpIndirect { pointer } => {
                emit_spill_a(&mut out);
                writeln!(out, "    ld hl, ${pointer:04X}").unwrap();
                writeln!(out, "    call nes_jmp_indirect_hl").unwrap();
                writeln!(out, "    jp nes_dispatch_hl").unwrap();
            }

            IrOp::Call { target, return_addr } => {
                emit_spill_a(&mut out);
                writeln!(out, "    ld hl, ${return_addr:04X}").unwrap();
                writeln!(out, "    call nes_stack_push_return_hl").unwrap();
                writeln!(out, "    ld hl, ${target:04X}").unwrap();
                writeln!(out, "    jp nes_dispatch_hl").unwrap();
            }

            IrOp::Return => {
                emit_spill_a(&mut out);
                writeln!(out, "    call nes_stack_pop_return_hl").unwrap();
                writeln!(out, "    inc hl").unwrap();
                writeln!(out, "    jp nes_dispatch_hl").unwrap();
            }

            IrOp::ReturnInterrupt => {
                emit_spill_a(&mut out);
                writeln!(out, "    call nes_rti_pop_hl").unwrap();
                writeln!(out, "    jp nes_dispatch_hl").unwrap();
            }

            IrOp::Break { return_pc } => {
                emit_spill_a(&mut out);
                writeln!(out, "    ld hl, ${return_pc:04X}").unwrap();
                writeln!(out, "    call nes_brk_hl").unwrap();
                writeln!(out, "    jp nes_irq_entry").unwrap();
            }

            IrOp::ReadIo { addr, dst } => {
                writeln!(out, "    ld hl, ${addr:04X}").unwrap();
                emit_preserved_b_call(&mut out, "nes_cpu_read");
                emit_a_to_reg(&mut out, dst);
                emit_update_nz(&mut out);
            }

            IrOp::WriteIo { addr, src } => {
                writeln!(out, "    ld hl, ${addr:04X}").unwrap();
                emit_reg_to_a(&mut out, src);
                emit_preserved_b_call(&mut out, "nes_cpu_write");
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
    fn accumulator_stays_in_b_for_hot_ops() {
        let asm = emit_ops(&[
            IrOp::Load { dst: Register::A, src: Operand::Immediate(0x12) },
            IrOp::Logic { op: LogicOp::Eor, rhs: Operand::Immediate(0x34) },
            IrOp::Store { src: Register::A, dst: Operand::ZeroPage(0x10) },
        ]);
        assert!(asm.contains("ld b, a"));
        assert!(asm.contains("ld a, b"));
        assert!(!asm.contains("ld a, [nes_a]"));
        assert!(!asm.contains("ld [nes_a], a"));
    }

    #[test]
    fn branches_read_shadow_status() {
        let asm = emit_ops(&[IrOp::Branch { flag: Flag::Negative, when: false, target: 0x9000 }]);
        assert!(asm.contains("ld a, [nes_p]"));
        assert!(asm.contains("and $80"));
        assert!(asm.contains("jr nz, :+"));
        assert!(asm.contains("ld hl, $9000"));
        assert!(asm.contains("jp nes_dispatch_hl"));
    }
}
