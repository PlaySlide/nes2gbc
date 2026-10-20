
use crate::mir::MirBuf;

use crate::ir::{ArithmeticOp, Flag, IrOp, LogicOp, ModifyOp, ModifyTarget, Operand, Register, StackValue};

pub const NES_RAM_BASE: u16 = 0xC000;

thread_local! {
    /// Test-only override of the APU setting (per test thread).
    static APU_OVERRIDE: std::cell::Cell<Option<bool>> = const { std::cell::Cell::new(None) };
}

/// NES APU emulation (runtime/apu.asm) is compiled in only with `APU=1`
/// (environment NES2GBC_APU=1, set by the Makefile). With APU=0, fixed APU
/// register writes emit nothing and $4015 reads return 0, exactly as before.
fn apu_enabled() -> bool {
    if let Some(v) = APU_OVERRIDE.with(|c| c.get()) {
        return v;
    }
    static APU: std::sync::OnceLock<bool> = std::sync::OnceLock::new();
    *APU.get_or_init(|| std::env::var("NES2GBC_APU").map(|v| v == "1").unwrap_or(false))
}

#[cfg(test)]
fn set_apu_for_test(v: bool) {
    APU_OVERRIDE.with(|c| c.set(Some(v)));
}

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

fn emit_add_a_to_hl(out: &mut MirBuf) {
    // Exact inline equivalent of runtime nes_add_a_to_hl. A contains the
    // unsigned 8-bit index and HL the base address.
    out.ins("add", &["l"], None);
    out.ins("ld", &["l", "a"], None);
    out.ins("jr", &["nc", ":+"], None);
    out.ins("inc", &["h"], None);
    out.label("");
}

fn emit_effective_addr(out: &mut MirBuf, op: Operand) -> bool {
    match op {
        Operand::ZeroPage(zp) => {
            out.ins("ld", &["h", "$C0"], None);
            out.ins("ld", &["l", format!("${zp:02X}").as_str()], None);
            true
        }
        Operand::ZeroPageX(zp) => {
            out.ins("ldh", &["a", "[nes_x]"], None);
            out.ins("add", &[format!("${zp:02X}").as_str()], None);
            out.ins("ld", &["l", "a"], None);
            out.ins("ld", &["h", "$C0"], None);
            true
        }
        Operand::ZeroPageY(zp) => {
            out.ins("ldh", &["a", "[nes_y]"], None);
            out.ins("add", &[format!("${zp:02X}").as_str()], None);
            out.ins("ld", &["l", "a"], None);
            out.ins("ld", &["h", "$C0"], None);
            true
        }
        Operand::Absolute(addr) => {
            if let Some(mapped) = direct_ram_addr(addr) {
                out.ins("ld", &["hl", format!("${mapped:04X}").as_str()], None);
                true
            } else {
                false
            }
        }
        Operand::AbsoluteX(addr) => {
            if addr < 0x0800 && addr + 0x00FF < 0x0800 {
                let mapped = NES_RAM_BASE + addr;
                out.ins("ld", &["hl", format!("${mapped:04X}").as_str()], None);
                out.ins("ldh", &["a", "[nes_x]"], None);
                emit_add_a_to_hl(out);
                true
            } else {
                false
            }
        }
        Operand::AbsoluteY(addr) => {
            if addr < 0x0800 && addr + 0x00FF < 0x0800 {
                let mapped = NES_RAM_BASE + addr;
                out.ins("ld", &["hl", format!("${mapped:04X}").as_str()], None);
                out.ins("ldh", &["a", "[nes_y]"], None);
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

fn emit_dynamic_cpu_read_hl(out: &mut MirBuf) {
    // Runtime profiling shows the overwhelming majority of dynamic CPU reads
    // resolve to $0000-$1FFF internal RAM. Handle that case inline and only
    // pay the generic bus helper for PPU/APU/PRG addresses.
    out.ins("ld", &["a", "h"], None);
    out.ins("cp", &["$20"], None);
    out.ins("jr", &["nc", ":+"], None);
    out.ins("and", &["$07"], None);
    out.ins("or", &["$C0"], None);
    out.ins("ld", &["h", "a"], None);
    out.ins("ld", &["a", "[hl]"], None);
    out.ins("jr", &[":++"], None);
    out.label("");
    out.ins("call", &["nes_cpu_read"], None);
    out.label("");
}

fn emit_dynamic_cpu_write_hl(out: &mut MirBuf) {
    // A = value, HL = dynamic NES CPU address. Most indexed writes land in
    // internal RAM too, so bypass the generic bus ladder for $0000-$1FFF.
    out.ins("ld", &["c", "a"], None);
    out.ins("ld", &["a", "h"], None);
    out.ins("cp", &["$20"], None);
    out.ins("jr", &["nc", ":+"], None);
    out.ins("and", &["$07"], None);
    out.ins("or", &["$C0"], None);
    out.ins("ld", &["h", "a"], None);
    out.ins("ld", &["a", "c"], None);
    out.ins("ld", &["[hl]", "a"], None);
    out.ins("jr", &[":++"], None);
    out.label("");
    out.ins("ld", &["a", "c"], None);
    out.ins("call", &["nes_cpu_write"], None);
    out.label("");
}

fn emit_indirect_addr(out: &mut MirBuf, src: Operand) {
    match src {
        Operand::IndexedIndirect(zp) => {
            out.ins("ldh", &["a", "[nes_x]"], None);
            out.ins("add", &[format!("${zp:02X}").as_str()], None);
            out.ins("ld", &["e", "a"], None);
            out.ins("ld", &["d", "$C0"], None);
            out.ins("ld", &["a", "[de]"], None);
            out.ins("ld", &["l", "a"], None);
            out.ins("inc", &["e"], None);
            out.ins("ld", &["a", "[de]"], None);
            out.ins("ld", &["h", "a"], None);
        }
        Operand::IndirectIndexed(zp) => {
            out.ins("ld", &["e", format!("${zp:02X}").as_str()], None);
            out.ins("ld", &["d", "$C0"], None);
            out.ins("ld", &["a", "[de]"], None);
            out.ins("ld", &["l", "a"], None);
            out.ins("inc", &["e"], None);
            out.ins("ld", &["a", "[de]"], None);
            out.ins("ld", &["h", "a"], None);
            out.ins("ldh", &["a", "[nes_y]"], None);
            emit_add_a_to_hl(out);
        }
        _ => unreachable!(),
    }
}
fn emit_load_operand_to_a(out: &mut MirBuf, src: Operand) {
    match src {
        Operand::Immediate(v) => { out.ins("ld", &["a", format!("${v:02X}").as_str()], None); }
        Operand::ZeroPage(zp) => {
            out.ins("ld", &["a", format!("[${:04X}]", NES_RAM_BASE + zp as u16).as_str()], None);
        }
        Operand::Absolute(addr) if direct_ram_addr(addr).is_some() => {
            out.ins("ld", &["a", format!("[${:04X}]", direct_ram_addr(addr).unwrap()).as_str()], None);
        }
        _ => {
            if emit_effective_addr(out, src) {
                out.ins("ld", &["a", "[hl]"], None);
            } else {
                match src {
                    Operand::Absolute(0x4016) => {
                        // Controller 1 serial port: skip the generic bus ladder.
                        out.ins("ld", &["hl", "$4016"], None);
                        out.ins("call", &["nes_controller_read"], None);
                    }
                    Operand::Absolute(0x4017) => {
                        // No controller 2: nes_cpu_read returns `xor a` here.
                        out.ins("ld", &["hl", "$4017"], None);
                        out.ins("xor", &["a"], Some("$4017 controller 2 not connected"));
                    }
                    Operand::Absolute(addr) => {
                        out.ins("ld", &["hl", format!("${addr:04X}").as_str()], None);
                        out.ins("call", &["nes_cpu_read"], None);
                    }
                    Operand::AbsoluteX(addr @ (0x4016 | 0x4017)) => {
                        out.ins("ld", &["hl", format!("${addr:04X}").as_str()], None);
                        out.ins("ldh", &["a", "[nes_x]"], None);
                        emit_add_a_to_hl(out);
                        out.ins("call", &["nes_cpu_read_joy_hl"], None);
                    }
                    Operand::AbsoluteY(addr @ (0x4016 | 0x4017)) => {
                        out.ins("ld", &["hl", format!("${addr:04X}").as_str()], None);
                        out.ins("ldh", &["a", "[nes_y]"], None);
                        emit_add_a_to_hl(out);
                        out.ins("call", &["nes_cpu_read_joy_hl"], None);
                    }
                    Operand::AbsoluteX(addr) => {
                        out.ins("ld", &["hl", format!("${addr:04X}").as_str()], None);
                        out.ins("ldh", &["a", "[nes_x]"], None);
                        emit_add_a_to_hl(out);
                        emit_dynamic_cpu_read_hl(out);
                    }
                    Operand::AbsoluteY(addr) => {
                        out.ins("ld", &["hl", format!("${addr:04X}").as_str()], None);
                        out.ins("ldh", &["a", "[nes_y]"], None);
                        emit_add_a_to_hl(out);
                        emit_dynamic_cpu_read_hl(out);
                    }
                    Operand::IndexedIndirect(_) | Operand::IndirectIndexed(_) => {
                        emit_indirect_addr(out, src);
                        emit_dynamic_cpu_read_hl(out);
                    }
                    _ => out.ins("call", &["nes_unimplemented_operand_read"], None),
                }
            }
        }
    }
}

fn emit_store_a_to_operand(out: &mut MirBuf, dst: Operand) {
    match dst {
        Operand::ZeroPage(zp) => {
            out.ins("ld", &[format!("[${:04X}]", NES_RAM_BASE + zp as u16).as_str(), "a"], None);
            return;
        }
        Operand::Absolute(addr) if direct_ram_addr(addr).is_some() => {
            out.ins("ld", &[format!("[${:04X}]", direct_ram_addr(addr).unwrap()).as_str(), "a"], None);
            return;
        }
        _ => {}
    }

    out.ins("push", &["af"], None);

    if emit_effective_addr(out, dst) {
        out.ins("pop", &["af"], None);
        out.ins("ld", &["[hl]", "a"], None);
        return;
    }

    let dynamic = match dst {
        Operand::Absolute(addr) => {
            out.ins("ld", &["hl", format!("${addr:04X}").as_str()], None);
            false
        }
        Operand::AbsoluteX(addr) => {
            out.ins("ld", &["hl", format!("${addr:04X}").as_str()], None);
            out.ins("ldh", &["a", "[nes_x]"], None);
            emit_add_a_to_hl(out);
            true
        }
        Operand::AbsoluteY(addr) => {
            out.ins("ld", &["hl", format!("${addr:04X}").as_str()], None);
            out.ins("ldh", &["a", "[nes_y]"], None);
            emit_add_a_to_hl(out);
            true
        }
        Operand::IndexedIndirect(_) | Operand::IndirectIndexed(_) => {
            emit_indirect_addr(out, dst);
            true
        }
        _ => {
            out.ins("pop", &["af"], None);
            out.ins("call", &["nes_unimplemented_operand_write"], None);
            return;
        }
    };

    out.ins("pop", &["af"], None);
    if dynamic {
        emit_dynamic_cpu_write_hl(out);
    } else {
        out.ins("call", &["nes_cpu_write"], None);
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

fn emit_update_nz(out: &mut MirBuf) {
    out.ins("ldh", &["[nes_z_shadow]", "a"], None);
    out.ins("ldh", &["[nes_n_shadow]", "a"], None);
}

pub fn emit_ops(ops: &[IrOp]) -> String {
    emit_ops_buf(ops).into_string()
}

/// Lower IR ops to MIR (see `emit_ops` for the text form).
pub fn emit_ops_buf(ops: &[IrOp]) -> MirBuf {
    let mut out = MirBuf::new();
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
                        out.ins("ld", &["a", format!("${:02X}", if value { 0x01 } else { 0x00 }).as_str()], None);
                        out.ins("ldh", &["[nes_c_shadow]", "a"], None);
                    }
                    Flag::Zero => {
                        out.ins("ld", &["a", format!("${:02X}", if value { 0x00 } else { 0x01 }).as_str()], None);
                        out.ins("ldh", &["[nes_z_shadow]", "a"], None);
                    }
                    Flag::Negative => {
                        out.ins("ld", &["a", format!("${:02X}", if value { 0x80 } else { 0x00 }).as_str()], None);
                        out.ins("ldh", &["[nes_n_shadow]", "a"], None);
                    }
                    _ => {
                        out.ins("ldh", &["a", "[nes_p]"], None);
                        if value {
                            out.ins("or", &[format!("${:02X}", flag_mask(flag)).as_str()], None);
                        } else {
                            out.ins("and", &[format!("${:02X}", !flag_mask(flag)).as_str()], None);
                        }
                        out.ins("ldh", &["[nes_p]", "a"], None);
                    }
                }
            }

            IrOp::Load { dst, src } => {
                emit_load_operand_to_a(&mut out, src);
                if dst == Register::A {
                    if commit_a_here {
                        out.ins("ldh", &["[nes_a]", "a"], None);
                    }
                } else {
                    out.ins("ldh", &[format!("[{}]", state_label(dst)).as_str(), "a"], None);
                }
                if update_nz_here { emit_update_nz(&mut out); }
                a_live = dst == Register::A;
            }

            IrOp::Store { src, dst } => {
                if src == Register::A {
                    if !a_live {
                        out.ins("ldh", &["a", "[nes_a]"], None);
                    }
                } else {
                    out.ins("ldh", &["a", format!("[{}]", state_label(src)).as_str()], None);
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
                        out.ins("ldh", &["a", "[nes_a]"], None);
                    }
                } else {
                    out.ins("ldh", &["a", format!("[{}]", state_label(src)).as_str()], None);
                }
                if dst == Register::A {
                    if commit_a_here {
                        out.ins("ldh", &["[nes_a]", "a"], None);
                    }
                } else {
                    out.ins("ldh", &[format!("[{}]", state_label(dst)).as_str(), "a"], None);
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
                        out.ins("ldh", &["a", "[nes_a]"], None);
                    }
                    out.ins("inc", &["a"], None);
                    if commit_a_here {
                        out.ins("ldh", &["[nes_a]", "a"], None);
                    }
                    if update_nz_here { emit_update_nz(&mut out); }
                    a_live = true;
                } else {
                    out.ins("ldh", &["a", format!("[{}]", state_label(reg)).as_str()], None);
                    out.ins("inc", &["a"], None);
                    out.ins("ldh", &[format!("[{}]", state_label(reg)).as_str(), "a"], None);
                    if update_nz_here { emit_update_nz(&mut out); }
                    a_live = false;
                }
            }

            IrOp::Dec(reg) => {
                if reg == Register::A {
                    if !a_live {
                        out.ins("ldh", &["a", "[nes_a]"], None);
                    }
                    out.ins("dec", &["a"], None);
                    if commit_a_here {
                        out.ins("ldh", &["[nes_a]", "a"], None);
                    }
                    if update_nz_here { emit_update_nz(&mut out); }
                    a_live = true;
                } else {
                    out.ins("ldh", &["a", format!("[{}]", state_label(reg)).as_str()], None);
                    out.ins("dec", &["a"], None);
                    out.ins("ldh", &[format!("[{}]", state_label(reg)).as_str(), "a"], None);
                    if update_nz_here { emit_update_nz(&mut out); }
                    a_live = false;
                }
            }

            IrOp::Logic { op, rhs } => {
                match rhs {
                    Operand::Immediate(imm) => {
                        if !a_live {
                            out.ins("ldh", &["a", "[nes_a]"], None);
                        }
                        match op {
                            LogicOp::And => out.ins("and", &[format!("${imm:02X}").as_str()], None),
                            LogicOp::Ora => out.ins("or", &[format!("${imm:02X}").as_str()], None),
                            LogicOp::Eor => out.ins("xor", &[format!("${imm:02X}").as_str()], None),
                        }
                    }
                    _ => {
                        if a_live {
                            out.ins("ld", &["c", "a"], None);
                            emit_load_operand_to_a(&mut out, rhs);
                            out.ins("ld", &["e", "a"], None);
                            out.ins("ld", &["a", "c"], None);
                        } else {
                            emit_load_operand_to_a(&mut out, rhs);
                            out.ins("ld", &["e", "a"], None);
                            out.ins("ldh", &["a", "[nes_a]"], None);
                        }
                        match op {
                            LogicOp::And => out.ins("and", &["e"], None),
                            LogicOp::Ora => out.ins("or", &["e"], None),
                            LogicOp::Eor => out.ins("xor", &["e"], None),
                        }
                    }
                }
                if commit_a_here {
                    out.ins("ldh", &["[nes_a]", "a"], None);
                }
                if update_nz_here { emit_update_nz(&mut out); }
                a_live = true;
            }

            IrOp::Arithmetic { op, rhs } => {
                match rhs {
                    Operand::Immediate(imm) => {
                        if !a_live {
                            out.ins("ldh", &["a", "[nes_a]"], None);
                        }
                        out.ins("ld", &["e", format!("${imm:02X}").as_str()], None);
                    }
                    _ => {
                        if a_live {
                            out.ins("ld", &["c", "a"], None);
                            emit_load_operand_to_a(&mut out, rhs);
                            out.ins("ld", &["e", "a"], None);
                            out.ins("ld", &["a", "c"], None);
                        } else {
                            emit_load_operand_to_a(&mut out, rhs);
                            out.ins("ld", &["e", "a"], None);
                            out.ins("ldh", &["a", "[nes_a]"], None);
                        }
                    }
                }
                match op {
                    ArithmeticOp::Adc => out.ins("call", &["nes_adc_a_e"], None),
                    ArithmeticOp::Sbc => out.ins("call", &["nes_sbc_a_e"], None),
                }
                if commit_a_here {
                    out.ins("ldh", &["[nes_a]", "a"], None);
                }
                a_live = true;
            }

            IrOp::Modify { op, target } => {
                let memory_target = match target {
                    ModifyTarget::Accumulator => {
                        if !a_live {
                            out.ins("ldh", &["a", "[nes_a]"], None);
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
                        out.ins("inc", &["a"], None);
                        if update_nz_here { emit_update_nz(&mut out); }
                    }
                    ModifyOp::Dec => {
                        out.ins("dec", &["a"], None);
                        if update_nz_here { emit_update_nz(&mut out); }
                    }
                    ModifyOp::Asl => {
                        out.ins("call", &["nes_asl_a"], None);
                    }
                    ModifyOp::Lsr => {
                        out.ins("call", &["nes_lsr_a"], None);
                    }
                    ModifyOp::Rol => {
                        out.ins("call", &["nes_rol_a"], None);
                    }
                    ModifyOp::Ror => {
                        out.ins("call", &["nes_ror_a"], None);
                    }
                }

                if let Some(mem) = memory_target {
                    emit_store_a_to_operand(&mut out, mem);
                    a_live = false;
                } else {
                    if commit_a_here {
                        out.ins("ldh", &["[nes_a]", "a"], None);
                    }
                    a_live = true;
                }
            }

            IrOp::Bit { rhs } => {
                match rhs {
                    Operand::Immediate(imm) => {
                        if !a_live {
                            out.ins("ldh", &["a", "[nes_a]"], None);
                        }
                        out.ins("ld", &["e", format!("${imm:02X}").as_str()], None);
                    }
                    _ => {
                        if a_live {
                            out.ins("ld", &["c", "a"], None);
                            emit_load_operand_to_a(&mut out, rhs);
                            out.ins("ld", &["e", "a"], None);
                            out.ins("ld", &["a", "c"], None);
                        } else {
                            emit_load_operand_to_a(&mut out, rhs);
                            out.ins("ld", &["e", "a"], None);
                            out.ins("ldh", &["a", "[nes_a]"], None);
                        }
                    }
                }
                out.ins("call", &["nes_bit_a_e"], None);
                a_live = false;
            }
            IrOp::Compare { reg, rhs } => {
                match rhs {
                    Operand::Immediate(imm) => {
                        out.ins("ld", &["e", format!("${imm:02X}").as_str()], None);
                        if reg == Register::A {
                            if !a_live {
                                out.ins("ldh", &["a", "[nes_a]"], None);
                            }
                        } else {
                            out.ins("ldh", &["a", format!("[{}]", state_label(reg)).as_str()], None);
                        }
                    }
                    _ => {
                        emit_load_operand_to_a(&mut out, rhs);
                        out.ins("ld", &["e", "a"], None);
                        out.ins("ldh", &["a", format!("[{}]", state_label(reg)).as_str()], None);
                    }
                }
                out.ins("call", &["nes_compare_a_e"], None);
                a_live = false;
            }

            IrOp::StackPush(StackValue::A) => {
                if !a_live {
                    out.ins("ldh", &["a", "[nes_a]"], None);
                }
                out.ins("call", &["nes_stack_push_a"], None);
                a_live = false;
            }
            IrOp::StackPush(StackValue::Status) => {
                out.ins("call", &["nes_materialize_p"], None);
                out.ins("or", &["$30"], None);
                out.ins("call", &["nes_stack_push_a"], None);
                a_live = false;
            }
            IrOp::StackPop(StackValue::A) => {
                out.ins("call", &["nes_stack_pop_a"], None);
                if commit_a_here {
                    out.ins("ldh", &["[nes_a]", "a"], None);
                }
                if update_nz_here { emit_update_nz(&mut out); }
                a_live = true;
            }
            IrOp::StackPop(StackValue::Status) => {
                out.ins("call", &["nes_stack_pop_a"], None);
                out.ins("call", &["nes_set_p_from_a"], None);
                a_live = false;
            }

            IrOp::Branch { flag, when, target } => {
                match flag {
                    Flag::Carry => {
                        out.ins("ldh", &["a", "[nes_c_shadow]"], None);
                        out.ins("and", &["a"], None);
                        out.ins("jr", &[format!("{}", if when { "z" } else { "nz" }).as_str(), ":+"], None);
                    }
                    Flag::Zero => {
                        out.ins("ldh", &["a", "[nes_z_shadow]"], None);
                        out.ins("and", &["a"], None);
                        out.ins("jr", &[format!("{}", if when { "nz" } else { "z" }).as_str(), ":+"], None);
                    }
                    Flag::Negative => {
                        out.ins("ldh", &["a", "[nes_n_shadow]"], None);
                        out.ins("bit", &["7", "a"], None);
                        out.ins("jr", &[format!("{}", if when { "z" } else { "nz" }).as_str(), ":+"], None);
                    }
                    _ => {
                        out.ins("ldh", &["a", "[nes_p]"], None);
                        out.ins("and", &[format!("${:02X}", flag_mask(flag)).as_str()], None);
                        out.ins("jr", &[format!("{}", if when { "z" } else { "nz" }).as_str(), ":+"], None);
                    }
                }
                out.ins("ld", &["hl", format!("${target:04X}").as_str()], None);
                out.ins("jp", &["nes_dispatch_hl"], None);
                out.label("");
                a_live = false;
            }

            IrOp::Jump(target) => {
                out.ins("ld", &["hl", format!("${target:04X}").as_str()], None);
                out.ins("jp", &["nes_dispatch_hl"], None);
                a_live = false;
            }

            IrOp::JumpIndirect { pointer } => {
                out.ins("ld", &["hl", format!("${pointer:04X}").as_str()], None);
                out.ins("call", &["nes_jmp_indirect_hl"], None);
                out.ins("jp", &["nes_dispatch_hl"], None);
                a_live = false;
            }

            IrOp::Call { target, return_addr } => {
                out.ins("ld", &["hl", format!("${return_addr:04X}").as_str()], None);
                out.ins("call", &["nes_stack_push_return_hl"], None);
                out.ins("ld", &["hl", format!("${target:04X}").as_str()], None);
                out.ins("jp", &["nes_dispatch_hl"], None);
                a_live = false;
            }

            IrOp::Return => {
                out.ins("call", &["nes_stack_pop_return_hl"], None);
                out.ins("inc", &["hl"], None);
                out.ins("jp", &["nes_dispatch_hl"], None);
                a_live = false;
            }

            IrOp::ReturnInterrupt => {
                out.ins("call", &["nes_rti_pop_hl"], None);
                out.ins("jp", &["nes_dispatch_hl"], None);
                a_live = false;
            }

            IrOp::Break { return_pc } => {
                out.ins("ld", &["hl", format!("${return_pc:04X}").as_str()], None);
                out.ins("call", &["nes_brk_hl"], None);
                out.ins("jp", &["nes_irq_entry"], None);
                a_live = false;
            }

            IrOp::ReadIo { addr, dst } => {
                match addr {
                    0x2000..=0x3FFF => {
                        out.ins("ld", &["l", format!("${:02X}", addr as u8 & 0x07).as_str()], None);
                        out.ins("call", &["nes_ppu_cpu_read"], None);
                    }
                    0x4011 => {
                        out.ins("ld", &["a", "[nes_dac]"], None);
                    }
                    0x4015 if apu_enabled() => {
                        out.ins("call", &["nes_apu_read_status"], None);
                    }
                    0x4016 => {
                        out.ins("call", &["nes_controller_read"], None);
                    }
                    0x4017 => {
                        // No controller 2: must match nes_cpu_read's `xor a`.
                        // A constant $01 makes games that OR both ports
                        // (Contra) see every button held.
                        out.ins("xor", &["a"], Some("$4017 controller 2 not connected"));
                    }
                    _ => {
                        out.ins("xor", &["a"], None);
                    }
                }
                if dst == Register::A {
                    if commit_a_here {
                        out.ins("ldh", &["[nes_a]", "a"], None);
                    }
                } else {
                    out.ins("ldh", &[format!("[{}]", state_label(dst)).as_str(), "a"], None);
                }
                if update_nz_here { emit_update_nz(&mut out); }
                a_live = dst == Register::A;
            }

            IrOp::WriteIo { addr, src } => {
                match addr {
                    0x2000..=0x3FFF => {
                        if src == Register::A {
                            if !a_live {
                                out.ins("ldh", &["a", "[nes_a]"], None);
                            }
                        } else {
                            out.ins("ldh", &["a", format!("[{}]", state_label(src)).as_str()], None);
                        }
                        out.ins("ld", &["e", "a"], None);
                        out.ins("ld", &["l", format!("${:02X}", addr as u8 & 0x07).as_str()], None);
                        out.ins("call", &["nes_ppu_cpu_write"], None);
                        a_live = false;
                    }
                    0x4011 => {
                        if src == Register::A {
                            if !a_live {
                                out.ins("ldh", &["a", "[nes_a]"], None);
                            }
                        } else {
                            out.ins("ldh", &["a", format!("[{}]", state_label(src)).as_str()], None);
                        }
                        out.ins("ld", &["[nes_dac]", "a"], None);
                        a_live = src == Register::A;
                    }
                    0x4014 => {
                        if src == Register::A {
                            if !a_live {
                                out.ins("ldh", &["a", "[nes_a]"], None);
                            }
                        } else {
                            out.ins("ldh", &["a", format!("[{}]", state_label(src)).as_str()], None);
                        }
                        out.ins("call", &["nes_oam_dma"], None);
                        a_live = false;
                    }
                    0x4016 => {
                        if src == Register::A {
                            if !a_live {
                                out.ins("ldh", &["a", "[nes_a]"], None);
                            }
                        } else {
                            out.ins("ldh", &["a", format!("[{}]", state_label(src)).as_str()], None);
                        }
                        out.ins("call", &["nes_controller_write"], None);
                        a_live = false;
                    }
                    0x4000..=0x4013 | 0x4015 | 0x4017 if apu_enabled() => {
                        if src == Register::A {
                            if !a_live {
                                out.ins("ldh", &["a", "[nes_a]"], None);
                            }
                        } else {
                            out.ins("ldh", &["a", format!("[{}]", state_label(src)).as_str()], None);
                        }
                        out.ins("ld", &["e", "a"], None);
                        out.ins("ld", &["l", format!("${:02X}", addr as u8).as_str()], None);
                        out.ins("call", &["nes_apu_write"], None);
                        a_live = false;
                    }
                    _ => {
                        // Other fixed IO writes (and APU writes with APU=0) emit nothing.
                    }
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
    fn fixed_apu_writes_emit_nothing_without_apu() {
        set_apu_for_test(false);
        let asm = emit_ops(&[
            IrOp::WriteIo { addr: 0x4000, src: Register::A },
            IrOp::WriteIo { addr: 0x4004, src: Register::X },
            IrOp::WriteIo { addr: 0x400C, src: Register::Y },
        ]);
        assert!(asm.is_empty());
    }

    #[test]
    fn fixed_apu_writes_call_nes_apu_write() {
        set_apu_for_test(true);
        let asm = emit_ops(&[
            IrOp::WriteIo { addr: 0x4000, src: Register::A },
            IrOp::WriteIo { addr: 0x4004, src: Register::X },
            IrOp::WriteIo { addr: 0x400C, src: Register::Y },
        ]);
        assert!(asm.contains("ld l, $00"));
        assert!(asm.contains("ld l, $04"));
        assert!(asm.contains("ld l, $0C"));
        assert!(asm.contains("call nes_apu_write"));
        assert!(!asm.contains("call nes_cpu_write"));
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
