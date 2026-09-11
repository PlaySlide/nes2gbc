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
            writeln!(out, "    ldh a, [nes_x]").unwrap();
            writeln!(out, "    add ${zp:02X}").unwrap();
            writeln!(out, "    ld l, a").unwrap();
            writeln!(out, "    ld h, $C0").unwrap();
            true
        }
        Operand::ZeroPageY(zp) => {
            writeln!(out, "    ldh a, [nes_y]").unwrap();
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
                writeln!(out, "    ldh a, [nes_x]").unwrap();
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
                writeln!(out, "    ldh a, [nes_y]").unwrap();
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

fn emit_dynamic_cpu_write_hl(out: &mut String) {
    // A = value, HL = dynamic NES CPU address. Most indexed writes land in
    // internal RAM too, so bypass the generic bus ladder for $0000-$1FFF.
    writeln!(out, "    ld c, a").unwrap();
    writeln!(out, "    ld a, h").unwrap();
    writeln!(out, "    cp $20").unwrap();
    writeln!(out, "    jr nc, :+").unwrap();
    writeln!(out, "    and $07").unwrap();
    writeln!(out, "    or $C0").unwrap();
    writeln!(out, "    ld h, a").unwrap();
    writeln!(out, "    ld a, c").unwrap();
    writeln!(out, "    ld [hl], a").unwrap();
    writeln!(out, "    jr :++").unwrap();
    writeln!(out, ":").unwrap();
    writeln!(out, "    ld a, c").unwrap();
    writeln!(out, "    call nes_cpu_write").unwrap();
    writeln!(out, ":").unwrap();
}

fn emit_indirect_addr(out: &mut String, src: Operand) {
    match src {
        Operand::IndexedIndirect(zp) => {
            writeln!(out, "    ldh a, [nes_x]").unwrap();
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
            writeln!(out, "    ldh a, [nes_y]").unwrap();
            emit_add_a_to_hl(out);
        }
        _ => unreachable!(),
    }
}
fn emit_load_operand_to_a(out: &mut String, src: Operand) {
    match src {
        Operand::Immediate(v) => { writeln!(out, "    ld a, ${v:02X}").unwrap(); }
        Operand::ZeroPage(zp) => {
            writeln!(out, "    ld a, [${:04X}]", NES_RAM_BASE + zp as u16).unwrap();
        }
        Operand::Absolute(addr) if direct_ram_addr(addr).is_some() => {
            writeln!(out, "    ld a, [${:04X}]", direct_ram_addr(addr).unwrap()).unwrap();
        }
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
                        writeln!(out, "    ldh a, [nes_x]").unwrap();
                        emit_add_a_to_hl(out);
                        emit_dynamic_cpu_read_hl(out);
                    }
                    Operand::AbsoluteY(addr) => {
                        writeln!(out, "    ld hl, ${addr:04X}").unwrap();
                        writeln!(out, "    ldh a, [nes_y]").unwrap();
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
    match dst {
        Operand::ZeroPage(zp) => {
            writeln!(out, "    ld [${:04X}], a", NES_RAM_BASE + zp as u16).unwrap();
            return;
        }
        Operand::Absolute(addr) if direct_ram_addr(addr).is_some() => {
            writeln!(out, "    ld [${:04X}], a", direct_ram_addr(addr).unwrap()).unwrap();
            return;
        }
        _ => {}
    }

    writeln!(out, "    push af").unwrap();

    if emit_effective_addr(out, dst) {
        writeln!(out, "    pop af").unwrap();
        writeln!(out, "    ld [hl], a").unwrap();
        return;
    }

    let dynamic = match dst {
        Operand::Absolute(addr) => {
            writeln!(out, "    ld hl, ${addr:04X}").unwrap();
            false
        }
        Operand::AbsoluteX(addr) => {
            writeln!(out, "    ld hl, ${addr:04X}").unwrap();
            writeln!(out, "    ldh a, [nes_x]").unwrap();
            emit_add_a_to_hl(out);
            true
        }
        Operand::AbsoluteY(addr) => {
            writeln!(out, "    ld hl, ${addr:04X}").unwrap();
            writeln!(out, "    ldh a, [nes_y]").unwrap();
            emit_add_a_to_hl(out);
            true
        }
        Operand::IndexedIndirect(_) | Operand::IndirectIndexed(_) => {
            emit_indirect_addr(out, dst);
            true
        }
        _ => {
            writeln!(out, "    pop af").unwrap();
            writeln!(out, "    call nes_unimplemented_operand_write").unwrap();
            return;
        }
    };

    writeln!(out, "    pop af").unwrap();
    if dynamic {
        emit_dynamic_cpu_write_hl(out);
    } else {
        writeln!(out, "    call nes_cpu_write").unwrap();
    }
}

fn op_writes_nz(op: &IrOp) -> bool {
    match op {
        IrOp::Load { .. }
        | IrOp::Logic { .. }
        | IrOp::Arithmetic { .. }
        | IrOp::Inc(_)
        | IrOp::Dec(_)
        | IrOp::Modify { .. }
        | IrOp::Bit { .. }
        | IrOp::Compare { .. }
        | IrOp::ReadIo { .. }
        | IrOp::Transfer { update_nz: true, .. } => true,
        IrOp::StackPop(StackValue::A) => true,
        IrOp::SetFlag { flag: Flag::Zero | Flag::Negative, .. } => true,
        _ => false,
    }
}

fn op_reads_nz(op: &IrOp) -> bool {
    match op {
        IrOp::Branch {
            flag: Flag::Zero | Flag::Negative,
            ..
        } => true,
        IrOp::StackPush(StackValue::Status) => true,
        // RTI/PHP-adjacent materialize; treat status materialize consumers as reads.
        IrOp::ReturnInterrupt => true,
        _ => false,
    }
}

fn op_escapes_flags(op: &IrOp) -> bool {
    matches!(
        op,
        IrOp::Jump(_)
            | IrOp::JumpIndirect { .. }
            | IrOp::Call { .. }
            | IrOp::Return
            | IrOp::ReturnInterrupt
            | IrOp::Break { .. }
    )
}

fn nz_updates_live(ops: &[IrOp]) -> Vec<bool> {
    let mut live = vec![false; ops.len()];
    for i in 0..ops.len() {
        if !op_writes_nz(&ops[i]) {
            continue;
        }
        // Conservatively keep the last NZ write in the batch (flags may escape
        // to a following branch emitted after flush, or the next block).
        let mut keep = true;
        for j in (i + 1)..ops.len() {
            if op_reads_nz(&ops[j]) {
                keep = true;
                break;
            }
            if op_writes_nz(&ops[j]) {
                keep = false;
                break;
            }
            if op_escapes_flags(&ops[j]) {
                keep = true;
                break;
            }
        }
        live[i] = keep;
    }
    live
}


fn op_updates_nes_a(op: &IrOp) -> bool {
    match op {
        IrOp::Load { dst: Register::A, .. }
        | IrOp::Logic { .. }
        | IrOp::Arithmetic { .. }
        | IrOp::Inc(Register::A)
        | IrOp::Dec(Register::A)
        | IrOp::Modify {
            target: ModifyTarget::Accumulator,
            ..
        }
        | IrOp::ReadIo { dst: Register::A, .. }
        | IrOp::StackPop(StackValue::A) => true,
        IrOp::Transfer { dst: Register::A, .. } => true,
        _ => false,
    }
}

/// Ops that destroy host A (or force a_live=false) without storing a fresh nes_a.
fn op_clobbers_a_without_nes_a_update(op: &IrOp) -> bool {
    if op_updates_nes_a(op) {
        return false;
    }
    match op {
        // Direct WRAM stores of A keep host A live.
        IrOp::Store {
            src: Register::A,
            dst: Operand::ZeroPage(_),
        } => false,
        IrOp::Store {
            src: Register::A,
            dst: Operand::Absolute(addr),
        } if direct_ram_addr(*addr).is_some() => false,
        IrOp::Nop => false,
        // Everything else may clobber A or leave the batch.
        _ => true,
    }
}

fn a_commits_live(ops: &[IrOp]) -> Vec<bool> {
    let mut live = vec![false; ops.len()];
    for i in 0..ops.len() {
        if !op_updates_nes_a(&ops[i]) {
            continue;
        }
        let mut keep = true;
        for j in (i + 1)..ops.len() {
            if op_clobbers_a_without_nes_a_update(&ops[j]) {
                keep = true;
                break;
            }
            if op_updates_nes_a(&ops[j]) {
                keep = false;
                break;
            }
            if op_escapes_flags(&ops[j]) {
                keep = true;
                break;
            }
        }
        live[i] = keep;
    }
    live
}

fn emit_update_nz(out: &mut String) {
    writeln!(out, "    ldh [nes_z_shadow], a").unwrap();
    writeln!(out, "    ldh [nes_n_shadow], a").unwrap();
}

pub fn emit_ops(ops: &[IrOp]) -> String {
    let mut out = String::new();
    // Host register A often already holds nes_a. Keep it live across ALU/store
    // chains instead of bouncing every result through HRAM.
    let mut a_live = false;
    let nz_live = nz_updates_live(ops);
    let a_commit = a_commits_live(ops);

    for (op_index, op) in ops.iter().enumerate() {
        let update_nz_here = nz_live[op_index];
        let commit_a_here = a_commit[op_index];
        match *op {
            IrOp::SetFlag { flag, value } => {
                a_live = false;
                match flag {
                    Flag::Carry => {
                        writeln!(out, "    ld a, ${:02X}", if value { 0x01 } else { 0x00 }).unwrap();
                        writeln!(out, "    ldh [nes_c_shadow], a").unwrap();
                    }
                    Flag::Zero => {
                        writeln!(out, "    ld a, ${:02X}", if value { 0x00 } else { 0x01 }).unwrap();
                        writeln!(out, "    ldh [nes_z_shadow], a").unwrap();
                    }
                    Flag::Negative => {
                        writeln!(out, "    ld a, ${:02X}", if value { 0x80 } else { 0x00 }).unwrap();
                        writeln!(out, "    ldh [nes_n_shadow], a").unwrap();
                    }
                    _ => {
                        writeln!(out, "    ldh a, [nes_p]").unwrap();
                        if value {
                            writeln!(out, "    or ${:02X}", flag_mask(flag)).unwrap();
                        } else {
                            writeln!(out, "    and ${:02X}", !flag_mask(flag)).unwrap();
                        }
                        writeln!(out, "    ldh [nes_p], a").unwrap();
                    }
                }
            }

            IrOp::Load { dst, src } => {
                emit_load_operand_to_a(&mut out, src);
                if dst == Register::A {
                    if commit_a_here {
                        writeln!(out, "    ldh [nes_a], a").unwrap();
                    }
                } else {
                    writeln!(out, "    ldh [{}], a", state_label(dst)).unwrap();
                }
                if update_nz_here { emit_update_nz(&mut out); }
                a_live = dst == Register::A;
            }

            IrOp::Store { src, dst } => {
                if src == Register::A {
                    if !a_live {
                        writeln!(out, "    ldh a, [nes_a]").unwrap();
                    }
                } else {
                    writeln!(out, "    ldh a, [{}]", state_label(src)).unwrap();
                }
                emit_store_a_to_operand(&mut out, dst);
                // Direct WRAM stores leave A intact; helper/call paths may clobber it.
                a_live = src == Register::A
                    && match dst {
                        Operand::ZeroPage(_) => true,
                        Operand::Absolute(addr) => direct_ram_addr(addr).is_some(),
                        _ => false,
                    };
            }

            IrOp::Transfer { src, dst, update_nz } => {
                if src == Register::A {
                    if !a_live {
                        writeln!(out, "    ldh a, [nes_a]").unwrap();
                    }
                } else {
                    writeln!(out, "    ldh a, [{}]", state_label(src)).unwrap();
                }
                if dst == Register::A {
                    if commit_a_here {
                        writeln!(out, "    ldh [nes_a], a").unwrap();
                    }
                } else {
                    writeln!(out, "    ldh [{}], a", state_label(dst)).unwrap();
                }
                if update_nz {
                    if update_nz_here { emit_update_nz(&mut out); }
                }
                // A still holds the transferred value for TAX/TAY/TXA/TYA.
                a_live = true;
            }

            IrOp::Inc(reg) => {
                if reg == Register::A {
                    if !a_live {
                        writeln!(out, "    ldh a, [nes_a]").unwrap();
                    }
                    writeln!(out, "    inc a").unwrap();
                    if commit_a_here {
                        writeln!(out, "    ldh [nes_a], a").unwrap();
                    }
                    if update_nz_here { emit_update_nz(&mut out); }
                    a_live = true;
                } else {
                    writeln!(out, "    ldh a, [{}]", state_label(reg)).unwrap();
                    writeln!(out, "    inc a").unwrap();
                    writeln!(out, "    ldh [{}], a", state_label(reg)).unwrap();
                    if update_nz_here { emit_update_nz(&mut out); }
                    a_live = false;
                }
            }

            IrOp::Dec(reg) => {
                if reg == Register::A {
                    if !a_live {
                        writeln!(out, "    ldh a, [nes_a]").unwrap();
                    }
                    writeln!(out, "    dec a").unwrap();
                    if commit_a_here {
                        writeln!(out, "    ldh [nes_a], a").unwrap();
                    }
                    if update_nz_here { emit_update_nz(&mut out); }
                    a_live = true;
                } else {
                    writeln!(out, "    ldh a, [{}]", state_label(reg)).unwrap();
                    writeln!(out, "    dec a").unwrap();
                    writeln!(out, "    ldh [{}], a", state_label(reg)).unwrap();
                    if update_nz_here { emit_update_nz(&mut out); }
                    a_live = false;
                }
            }

            IrOp::Logic { op, rhs } => {
                match rhs {
                    Operand::Immediate(imm) => {
                        if !a_live {
                            writeln!(out, "    ldh a, [nes_a]").unwrap();
                        }
                        match op {
                            LogicOp::And => writeln!(out, "    and ${imm:02X}").unwrap(),
                            LogicOp::Ora => writeln!(out, "    or ${imm:02X}").unwrap(),
                            LogicOp::Eor => writeln!(out, "    xor ${imm:02X}").unwrap(),
                        }
                    }
                    _ => {
                        if a_live {
                            writeln!(out, "    ld c, a").unwrap();
                            emit_load_operand_to_a(&mut out, rhs);
                            writeln!(out, "    ld e, a").unwrap();
                            writeln!(out, "    ld a, c").unwrap();
                        } else {
                            emit_load_operand_to_a(&mut out, rhs);
                            writeln!(out, "    ld e, a").unwrap();
                            writeln!(out, "    ldh a, [nes_a]").unwrap();
                        }
                        match op {
                            LogicOp::And => writeln!(out, "    and e").unwrap(),
                            LogicOp::Ora => writeln!(out, "    or e").unwrap(),
                            LogicOp::Eor => writeln!(out, "    xor e").unwrap(),
                        }
                    }
                }
                if commit_a_here {
                    writeln!(out, "    ldh [nes_a], a").unwrap();
                }
                if update_nz_here { emit_update_nz(&mut out); }
                a_live = true;
            }

            IrOp::Arithmetic { op, rhs } => {
                match rhs {
                    Operand::Immediate(imm) => {
                        if !a_live {
                            writeln!(out, "    ldh a, [nes_a]").unwrap();
                        }
                        writeln!(out, "    ld e, ${imm:02X}").unwrap();
                    }
                    _ => {
                        if a_live {
                            writeln!(out, "    ld c, a").unwrap();
                            emit_load_operand_to_a(&mut out, rhs);
                            writeln!(out, "    ld e, a").unwrap();
                            writeln!(out, "    ld a, c").unwrap();
                        } else {
                            emit_load_operand_to_a(&mut out, rhs);
                            writeln!(out, "    ld e, a").unwrap();
                            writeln!(out, "    ldh a, [nes_a]").unwrap();
                        }
                    }
                }
                match op {
                    ArithmeticOp::Adc => writeln!(out, "    call nes_adc_a_e").unwrap(),
                    ArithmeticOp::Sbc => writeln!(out, "    call nes_sbc_a_e").unwrap(),
                }
                if commit_a_here {
                    writeln!(out, "    ldh [nes_a], a").unwrap();
                }
                a_live = true;
            }

            IrOp::Modify { op, target } => {
                let memory_target = match target {
                    ModifyTarget::Accumulator => {
                        if !a_live {
                            writeln!(out, "    ldh a, [nes_a]").unwrap();
                        }
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
                        if update_nz_here { emit_update_nz(&mut out); }
                    }
                    ModifyOp::Dec => {
                        writeln!(out, "    dec a").unwrap();
                        if update_nz_here { emit_update_nz(&mut out); }
                    }
                    ModifyOp::Asl => {
                        writeln!(out, "    call nes_asl_a").unwrap();
                    }
                    ModifyOp::Lsr => {
                        writeln!(out, "    call nes_lsr_a").unwrap();
                    }
                    ModifyOp::Rol => {
                        writeln!(out, "    call nes_rol_a").unwrap();
                    }
                    ModifyOp::Ror => {
                        writeln!(out, "    call nes_ror_a").unwrap();
                    }
                }

                if let Some(mem) = memory_target {
                    emit_store_a_to_operand(&mut out, mem);
                    a_live = false;
                } else {
                    if commit_a_here {
                        writeln!(out, "    ldh [nes_a], a").unwrap();
                    }
                    a_live = true;
                }
            }

            IrOp::Bit { rhs } => {
                match rhs {
                    Operand::Immediate(imm) => {
                        if !a_live {
                            writeln!(out, "    ldh a, [nes_a]").unwrap();
                        }
                        writeln!(out, "    ld e, ${imm:02X}").unwrap();
                    }
                    _ => {
                        if a_live {
                            writeln!(out, "    ld c, a").unwrap();
                            emit_load_operand_to_a(&mut out, rhs);
                            writeln!(out, "    ld e, a").unwrap();
                            writeln!(out, "    ld a, c").unwrap();
                        } else {
                            emit_load_operand_to_a(&mut out, rhs);
                            writeln!(out, "    ld e, a").unwrap();
                            writeln!(out, "    ldh a, [nes_a]").unwrap();
                        }
                    }
                }
                writeln!(out, "    call nes_bit_a_e").unwrap();
                a_live = false;
            }
            IrOp::Compare { reg, rhs } => {
                match rhs {
                    Operand::Immediate(imm) => {
                        writeln!(out, "    ld e, ${imm:02X}").unwrap();
                        if reg == Register::A {
                            if !a_live {
                                writeln!(out, "    ldh a, [nes_a]").unwrap();
                            }
                        } else {
                            writeln!(out, "    ldh a, [{}]", state_label(reg)).unwrap();
                        }
                    }
                    _ => {
                        emit_load_operand_to_a(&mut out, rhs);
                        writeln!(out, "    ld e, a").unwrap();
                        writeln!(out, "    ldh a, [{}]", state_label(reg)).unwrap();
                    }
                }
                writeln!(out, "    call nes_compare_a_e").unwrap();
                a_live = false;
            }

            IrOp::StackPush(StackValue::A) => {
                if !a_live {
                    writeln!(out, "    ldh a, [nes_a]").unwrap();
                }
                writeln!(out, "    call nes_stack_push_a").unwrap();
                a_live = false;
            }
            IrOp::StackPush(StackValue::Status) => {
                writeln!(out, "    call nes_materialize_p").unwrap();
                writeln!(out, "    or $30").unwrap();
                writeln!(out, "    call nes_stack_push_a").unwrap();
                a_live = false;
            }
            IrOp::StackPop(StackValue::A) => {
                writeln!(out, "    call nes_stack_pop_a").unwrap();
                if commit_a_here {
                    writeln!(out, "    ldh [nes_a], a").unwrap();
                }
                if update_nz_here { emit_update_nz(&mut out); }
                a_live = true;
            }
            IrOp::StackPop(StackValue::Status) => {
                writeln!(out, "    call nes_stack_pop_a").unwrap();
                writeln!(out, "    call nes_set_p_from_a").unwrap();
                a_live = false;
            }

            IrOp::Branch { flag, when, target } => {
                match flag {
                    Flag::Carry => {
                        writeln!(out, "    ldh a, [nes_c_shadow]").unwrap();
                        writeln!(out, "    and a").unwrap();
                        writeln!(out, "    jr {}, :+", if when { "z" } else { "nz" }).unwrap();
                    }
                    Flag::Zero => {
                        writeln!(out, "    ldh a, [nes_z_shadow]").unwrap();
                        writeln!(out, "    and a").unwrap();
                        writeln!(out, "    jr {}, :+", if when { "nz" } else { "z" }).unwrap();
                    }
                    Flag::Negative => {
                        writeln!(out, "    ldh a, [nes_n_shadow]").unwrap();
                        writeln!(out, "    bit 7, a").unwrap();
                        writeln!(out, "    jr {}, :+", if when { "z" } else { "nz" }).unwrap();
                    }
                    _ => {
                        writeln!(out, "    ldh a, [nes_p]").unwrap();
                        writeln!(out, "    and ${:02X}", flag_mask(flag)).unwrap();
                        writeln!(out, "    jr {}, :+", if when { "z" } else { "nz" }).unwrap();
                    }
                }
                writeln!(out, "    ld hl, ${target:04X}").unwrap();
                writeln!(out, "    jp nes_dispatch_hl").unwrap();
                writeln!(out, ":").unwrap();
                a_live = false;
            }

            IrOp::Jump(target) => {
                writeln!(out, "    ld hl, ${target:04X}").unwrap();
                writeln!(out, "    jp nes_dispatch_hl").unwrap();
                a_live = false;
            }

            IrOp::JumpIndirect { pointer } => {
                writeln!(out, "    ld hl, ${pointer:04X}").unwrap();
                writeln!(out, "    call nes_jmp_indirect_hl").unwrap();
                writeln!(out, "    jp nes_dispatch_hl").unwrap();
                a_live = false;
            }

            IrOp::Call { target, return_addr } => {
                writeln!(out, "    ld hl, ${return_addr:04X}").unwrap();
                writeln!(out, "    call nes_stack_push_return_hl").unwrap();
                writeln!(out, "    ld hl, ${target:04X}").unwrap();
                writeln!(out, "    jp nes_dispatch_hl").unwrap();
                a_live = false;
            }

            IrOp::Return => {
                writeln!(out, "    call nes_stack_pop_return_hl").unwrap();
                writeln!(out, "    inc hl").unwrap();
                writeln!(out, "    jp nes_dispatch_hl").unwrap();
                a_live = false;
            }

            IrOp::ReturnInterrupt => {
                writeln!(out, "    call nes_rti_pop_hl").unwrap();
                writeln!(out, "    jp nes_dispatch_hl").unwrap();
                a_live = false;
            }

            IrOp::Break { return_pc } => {
                writeln!(out, "    ld hl, ${return_pc:04X}").unwrap();
                writeln!(out, "    call nes_brk_hl").unwrap();
                writeln!(out, "    jp nes_irq_entry").unwrap();
                a_live = false;
            }

            IrOp::ReadIo { addr, dst } => {
                match addr {
                    0x2000..=0x3FFF => {
                        writeln!(out, "    ld l, ${:02X}", addr as u8 & 0x07).unwrap();
                        writeln!(out, "    call nes_ppu_cpu_read").unwrap();
                    }
                    0x4011 => {
                        writeln!(out, "    ld a, [nes_dac]").unwrap();
                    }
                    0x4016 => {
                        writeln!(out, "    call nes_controller_read").unwrap();
                    }
                    0x4017 => {
                        writeln!(out, "    ld a, $01").unwrap();
                    }
                    _ => {
                        writeln!(out, "    xor a").unwrap();
                    }
                }
                if dst == Register::A {
                    if commit_a_here {
                        writeln!(out, "    ldh [nes_a], a").unwrap();
                    }
                } else {
                    writeln!(out, "    ldh [{}], a", state_label(dst)).unwrap();
                }
                if update_nz_here { emit_update_nz(&mut out); }
                a_live = dst == Register::A;
            }

            IrOp::WriteIo { addr, src } => {
                match addr {
                    0x2000..=0x3FFF => {
                        if src == Register::A {
                            if !a_live {
                                writeln!(out, "    ldh a, [nes_a]").unwrap();
                            }
                        } else {
                            writeln!(out, "    ldh a, [{}]", state_label(src)).unwrap();
                        }
                        writeln!(out, "    ld e, a").unwrap();
                        writeln!(out, "    ld l, ${:02X}", addr as u8 & 0x07).unwrap();
                        writeln!(out, "    call nes_ppu_cpu_write").unwrap();
                        a_live = false;
                    }
                    0x4011 => {
                        if src == Register::A {
                            if !a_live {
                                writeln!(out, "    ldh a, [nes_a]").unwrap();
                            }
                        } else {
                            writeln!(out, "    ldh a, [{}]", state_label(src)).unwrap();
                        }
                        writeln!(out, "    ld [nes_dac], a").unwrap();
                        a_live = src == Register::A;
                    }
                    0x4014 => {
                        if src == Register::A {
                            if !a_live {
                                writeln!(out, "    ldh a, [nes_a]").unwrap();
                            }
                        } else {
                            writeln!(out, "    ldh a, [{}]", state_label(src)).unwrap();
                        }
                        writeln!(out, "    call nes_oam_dma").unwrap();
                        a_live = false;
                    }
                    0x4016 => {
                        if src == Register::A {
                            if !a_live {
                                writeln!(out, "    ldh a, [nes_a]").unwrap();
                            }
                        } else {
                            writeln!(out, "    ldh a, [{}]", state_label(src)).unwrap();
                        }
                        writeln!(out, "    call nes_controller_write").unwrap();
                        a_live = false;
                    }
                    _ => {}
                }
            }

            IrOp::Nop => {}
        }
    }

    let _ = a_live;
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
        assert!(!asm.contains("ld h, $C0"));
    }

    #[test]
    fn ignored_fixed_apu_writes_emit_nothing() {
        let asm = emit_ops(&[
            IrOp::WriteIo { addr: 0x4000, src: Register::A },
            IrOp::WriteIo { addr: 0x4004, src: Register::X },
            IrOp::WriteIo { addr: 0x400C, src: Register::Y },
        ]);
        assert!(asm.is_empty());
    }

    #[test]
    fn fixed_io_accesses_bypass_generic_cpu_bus() {
        let asm = emit_ops(&[
            IrOp::ReadIo { addr: 0x2002, dst: Register::A },
            IrOp::WriteIo { addr: 0x2007, src: Register::A },
            IrOp::ReadIo { addr: 0x4016, dst: Register::A },
            IrOp::WriteIo { addr: 0x4014, src: Register::A },
        ]);
        assert!(asm.contains("call nes_ppu_cpu_read"));
        assert!(asm.contains("call nes_ppu_cpu_write"));
        assert!(asm.contains("call nes_controller_read"));
        assert!(asm.contains("call nes_oam_dma"));
        assert!(!asm.contains("call nes_cpu_read"));
        assert!(!asm.contains("call nes_cpu_write"));
    }

    #[test]
    fn fixed_absolute_internal_ram_uses_direct_wram_access() {
        let asm = emit_ops(&[
            IrOp::Load { dst: Register::A, src: Operand::Absolute(0x0234) },
            IrOp::Store { src: Register::A, dst: Operand::Absolute(0x17AB) },
        ]);
        assert!(asm.contains("ld a, [$C234]"));
        assert!(asm.contains("ld [$C7AB], a"));
        assert!(!asm.contains("ld hl, $C234"));
        assert!(!asm.contains("ld hl, $C7AB"));
    }

    #[test]
    fn zero_page_load_store_use_direct_wram_without_hl_setup() {
        let asm = emit_ops(&[
            IrOp::Load { dst: Register::A, src: Operand::ZeroPage(0x12) },
            IrOp::Store { src: Register::A, dst: Operand::ZeroPage(0x34) },
        ]);
        assert!(asm.contains("ld a, [$C012]"));
        assert!(asm.contains("ld [$C034], a"));
        assert!(!asm.contains("ld h, $C0"));
    }

    #[test]
    fn dynamic_indexed_writes_fast_path_internal_ram() {
        let asm = emit_ops(&[
            IrOp::Store { src: Register::A, dst: Operand::IndirectIndexed(0x10) },
        ]);
        assert!(asm.contains("ld c, a"));
        assert!(asm.contains("cp $20"));
        assert!(asm.contains("and $07"));
        assert!(asm.contains("or $C0"));
        assert!(asm.contains("ld [hl], a"));
        assert!(asm.contains("call nes_cpu_write"));
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
    fn carry_set_clear_and_branches_use_lazy_shadow() {
        let asm = emit_ops(&[
            IrOp::SetFlag { flag: Flag::Carry, value: true },
            IrOp::SetFlag { flag: Flag::Carry, value: false },
            IrOp::Branch { flag: Flag::Carry, when: true, target: 0x9000 },
        ]);
        assert!(asm.contains("ldh [nes_c_shadow], a"));
        assert!(asm.contains("ldh a, [nes_c_shadow]"));
        assert!(!asm.contains("and $01"));
    }

    #[test]
    fn zero_and_negative_branches_use_lazy_shadows() {
        let asm = emit_ops(&[
            IrOp::Branch { flag: Flag::Negative, when: false, target: 0x9000 },
            IrOp::Branch { flag: Flag::Zero, when: true, target: 0x9010 },
        ]);
        assert!(asm.contains("ldh a, [nes_n_shadow]"));
        assert!(asm.contains("bit 7, a"));
        assert!(asm.contains("ldh a, [nes_z_shadow]"));
        assert!(asm.contains("and a"));
        assert!(asm.contains("ld hl, $9000"));
        assert!(asm.contains("ld hl, $9010"));
    }

    #[test]
    fn nz_updates_are_inlined_without_helper_call() {
        let asm = emit_ops(&[IrOp::Load { dst: Register::A, src: Operand::Immediate(0) }]);
        assert!(asm.contains("ldh [nes_z_shadow], a"));
        assert!(asm.contains("ldh [nes_n_shadow], a"));
        assert!(!asm.contains("call nes_set_nz_from_a"));
    }

    #[test]
    fn live_a_skips_hram_reload_on_and_imm_and_store() {
        let asm = emit_ops(&[
            IrOp::Load { dst: Register::A, src: Operand::Absolute(0x0778) },
            IrOp::Logic { op: LogicOp::And, rhs: Operand::Immediate(0x7F) },
            IrOp::Store { src: Register::A, dst: Operand::Absolute(0x0778) },
        ]);
        assert!(asm.contains("and $7F"));
        assert!(!asm.contains("ld e, a"));
        // One HRAM load of nes_a at most — should be zero after live-A load.
        assert_eq!(asm.matches("ldh a, [nes_a]").count(), 0);
        assert!(asm.contains("ld [$C778], a"));
        // LDA NZ is dead (AND overwrites); AND NZ stays (escapes batch).
        assert_eq!(asm.matches("ldh [nes_z_shadow], a").count(), 1);
        assert_eq!(asm.matches("ldh [nes_n_shadow], a").count(), 1);
        // LDA's nes_a commit is dead (AND commits before store/end).
        assert_eq!(asm.matches("ldh [nes_a], a").count(), 1);
    }

    #[test]
    fn dead_nz_elided_when_overwritten_before_use() {
        let asm = emit_ops(&[
            IrOp::Load { dst: Register::A, src: Operand::Immediate(1) },
            IrOp::Load { dst: Register::A, src: Operand::Immediate(0) },
        ]);
        // Only the second load's NZ is live-out.
        assert_eq!(asm.matches("ldh [nes_z_shadow], a").count(), 1);
        assert_eq!(asm.matches("ldh [nes_n_shadow], a").count(), 1);
    }
}
