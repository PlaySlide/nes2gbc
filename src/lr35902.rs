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

fn emit_add_a_to_hl(out: &mut String) {
    // Exact inline equivalent of runtime nes_add_a_to_hl. A contains the
    // unsigned 8-bit index and HL the base address.
    writeln!(out, "    add l").unwrap();
    writeln!(out, "    ld l, a").unwrap();
    writeln!(out, "    jr nc, :+").unwrap();
    writeln!(out, "    inc h").unwrap();
    writeln!(out, ":").unwrap();
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
                emit_add_a_to_hl(out);
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
                emit_add_a_to_hl(out);
                true
            } else {
                false
            }
        }
        Operand::IndexedIndirect(_) | Operand::IndirectIndexed(_) => false,
        Operand::Immediate(_) => false,
    }
}

fn emit_dynamic_cpu_read_hl(out: &mut String) {
    // Runtime profiling shows the overwhelming majority of dynamic CPU reads
    // resolve to $0000-$1FFF internal RAM. Handle that case inline and only
    // pay the generic bus helper for PPU/APU/PRG addresses.
    writeln!(out, "    ld a, h").unwrap();
    writeln!(out, "    cp $20").unwrap();
    writeln!(out, "    jr nc, :+").unwrap();
    writeln!(out, "    and $07").unwrap();
    writeln!(out, "    or $C0").unwrap();
    writeln!(out, "    ld h, a").unwrap();
    writeln!(out, "    ld a, [hl]").unwrap();
    writeln!(out, "    jr :++").unwrap();
    writeln!(out, ":").unwrap();
    writeln!(out, "    call nes_cpu_read").unwrap();
    writeln!(out, ":").unwrap();
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
            emit_add_a_to_hl(out);
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
                        writeln!(out, "    call nes_cpu_read").unwrap();
                    }
                    Operand::AbsoluteX(addr) => {
                        writeln!(out, "    ld hl, ${addr:04X}").unwrap();
                        writeln!(out, "    ld a, [nes_x]").unwrap();
                        emit_add_a_to_hl(out);
                        emit_dynamic_cpu_read_hl(out);
                    }
                    Operand::AbsoluteY(addr) => {
                        writeln!(out, "    ld hl, ${addr:04X}").unwrap();
                        writeln!(out, "    ld a, [nes_y]").unwrap();
                        emit_add_a_to_hl(out);
                        emit_dynamic_cpu_read_hl(out);
                    }
                    Operand::IndexedIndirect(_) | Operand::IndirectIndexed(_) => {
                        emit_indirect_addr(out, src);
                        emit_dynamic_cpu_read_hl(out);
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
            emit_add_a_to_hl(out);
        }
        Operand::AbsoluteY(addr) => {
            writeln!(out, "    ld hl, ${addr:04X}").unwrap();
            writeln!(out, "    ld a, [nes_y]").unwrap();
            emit_add_a_to_hl(out);
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
    writeln!(out, "    call nes_cpu_write").unwrap();
}
fn emit_update_nz(out: &mut String) {
    writeln!(out, "    ld [nes_z_shadow], a").unwrap();
    writeln!(out, "    ld [nes_n_shadow], a").unwrap();
}

pub fn emit_ops(ops: &[IrOp]) -> String {
    let mut out = String::new();

    for op in ops {
        match *op {
            IrOp::SetFlag { flag, value } => {
                match flag {
                    Flag::Zero => {
                        writeln!(out, "    ld a, ${:02X}", if value { 0x00 } else { 0x01 }).unwrap();
                        writeln!(out, "    ld [nes_z_shadow], a").unwrap();
                    }
                    Flag::Negative => {
                        writeln!(out, "    ld a, ${:02X}", if value { 0x80 } else { 0x00 }).unwrap();
                        writeln!(out, "    ld [nes_n_shadow], a").unwrap();
                    }
                    _ => {
                        writeln!(out, "    ld a, [nes_p]").unwrap();
                        if value {
                            writeln!(out, "    or ${:02X}", flag_mask(flag)).unwrap();
                        } else {
                            writeln!(out, "    and ${:02X}", !flag_mask(flag)).unwrap();
                        }
                        writeln!(out, "    ld [nes_p], a").unwrap();
                    }
                }
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

            IrOp::Arithmetic { op, rhs } => {
                emit_load_operand_to_a(&mut out, rhs);
                writeln!(out, "    ld e, a").unwrap();
                writeln!(out, "    ld a, [nes_a]").unwrap();
                match op {
                    ArithmeticOp::Adc => writeln!(out, "    call nes_adc_a_e").unwrap(),
                    ArithmeticOp::Sbc => writeln!(out, "    call nes_sbc_a_e").unwrap(),
                }
                writeln!(out, "    ld [nes_a], a").unwrap();
            }

            IrOp::Modify { op, target } => {
                let memory_target = match target {
                    ModifyTarget::Accumulator => {
                        writeln!(out, "    ld a, [nes_a]").unwrap();
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
                    writeln!(out, "    ld [nes_a], a").unwrap();
                }
            }

            IrOp::Bit { rhs } => {
                emit_load_operand_to_a(&mut out, rhs);
                writeln!(out, "    ld e, a").unwrap();
                writeln!(out, "    ld a, [nes_a]").unwrap();
                writeln!(out, "    call nes_bit_a_e").unwrap();
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
                writeln!(out, "    call nes_materialize_p").unwrap();
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
                writeln!(out, "    call nes_set_p_from_a").unwrap();
            }

            IrOp::Branch { flag, when, target } => {
                match flag {
                    Flag::Zero => {
                        writeln!(out, "    ld a, [nes_z_shadow]").unwrap();
                        writeln!(out, "    and a").unwrap();
                        writeln!(out, "    jr {}, :+", if when { "nz" } else { "z" }).unwrap();
                    }
                    Flag::Negative => {
                        writeln!(out, "    ld a, [nes_n_shadow]").unwrap();
                        writeln!(out, "    bit 7, a").unwrap();
                        writeln!(out, "    jr {}, :+", if when { "z" } else { "nz" }).unwrap();
                    }
                    _ => {
                        writeln!(out, "    ld a, [nes_p]").unwrap();
                        writeln!(out, "    and ${:02X}", flag_mask(flag)).unwrap();
                        writeln!(out, "    jr {}, :+", if when { "z" } else { "nz" }).unwrap();
                    }
                }
                writeln!(out, "    ld hl, ${target:04X}").unwrap();
                writeln!(out, "    jp nes_dispatch_hl").unwrap();
                writeln!(out, ":").unwrap();
            }

            IrOp::Jump(target) => {
                writeln!(out, "    ld hl, ${target:04X}").unwrap();
                writeln!(out, "    jp nes_dispatch_hl").unwrap();
            }

            IrOp::JumpIndirect { pointer } => {
                writeln!(out, "    ld hl, ${pointer:04X}").unwrap();
                writeln!(out, "    call nes_jmp_indirect_hl").unwrap();
                writeln!(out, "    jp nes_dispatch_hl").unwrap();
            }

            IrOp::Call { target, return_addr } => {
                writeln!(out, "    ld hl, ${return_addr:04X}").unwrap();
                writeln!(out, "    call nes_stack_push_return_hl").unwrap();
                writeln!(out, "    ld hl, ${target:04X}").unwrap();
                writeln!(out, "    jp nes_dispatch_hl").unwrap();
            }

            IrOp::Return => {
                writeln!(out, "    call nes_stack_pop_return_hl").unwrap();
                writeln!(out, "    inc hl").unwrap();
                writeln!(out, "    jp nes_dispatch_hl").unwrap();
            }

            IrOp::ReturnInterrupt => {
                writeln!(out, "    call nes_rti_pop_hl").unwrap();
                writeln!(out, "    jp nes_dispatch_hl").unwrap();
            }

            IrOp::Break { return_pc } => {
                writeln!(out, "    ld hl, ${return_pc:04X}").unwrap();
                writeln!(out, "    call nes_brk_hl").unwrap();
                writeln!(out, "    jp nes_irq_entry").unwrap();
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
    fn dynamic_indexed_reads_fast_path_internal_ram() {
        let asm = emit_ops(&[
            IrOp::Load { dst: Register::A, src: Operand::IndirectIndexed(0x10) },
        ]);
        assert!(asm.contains("cp $20"));
        assert!(asm.contains("and $07"));
        assert!(asm.contains("or $C0"));
        assert!(asm.contains("ld a, [hl]"));
        assert!(asm.contains("call nes_cpu_read"));
    }

    #[test]
    fn indexed_addressing_inlines_add_to_hl() {
        let asm = emit_ops(&[
            IrOp::Load { dst: Register::A, src: Operand::AbsoluteX(0x0200) },
            IrOp::Load { dst: Register::A, src: Operand::IndirectIndexed(0x10) },
        ]);
        assert!(asm.contains("add l"));
        assert!(asm.contains("ld l, a"));
        assert!(!asm.contains("call nes_add_a_to_hl"));
    }

    #[test]
    fn zero_and_negative_branches_use_lazy_shadows() {
        let asm = emit_ops(&[
            IrOp::Branch { flag: Flag::Negative, when: false, target: 0x9000 },
            IrOp::Branch { flag: Flag::Zero, when: true, target: 0x9010 },
        ]);
        assert!(asm.contains("ld a, [nes_n_shadow]"));
        assert!(asm.contains("bit 7, a"));
        assert!(asm.contains("ld a, [nes_z_shadow]"));
        assert!(asm.contains("and a"));
        assert!(asm.contains("ld hl, $9000"));
        assert!(asm.contains("ld hl, $9010"));
    }

    #[test]
    fn nz_updates_are_inlined_without_helper_call() {
        let asm = emit_ops(&[IrOp::Load { dst: Register::A, src: Operand::Immediate(0) }]);
        assert!(asm.contains("ld [nes_z_shadow], a"));
        assert!(asm.contains("ld [nes_n_shadow], a"));
        assert!(!asm.contains("call nes_set_nz_from_a"));
    }
}
