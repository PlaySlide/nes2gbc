use std::fmt;

use crate::cpu6502::{AddressingMode, DecodedInstruction, Mnemonic};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Register { A, X, Y, Sp }

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Flag { Carry, Zero, InterruptDisable, Decimal, Overflow, Negative }

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Operand {
    Immediate(u8),
    ZeroPage(u8),
    ZeroPageX(u8),
    ZeroPageY(u8),
    Absolute(u16),
    AbsoluteX(u16),
    AbsoluteY(u16),
    IndexedIndirect(u8),
    IndirectIndexed(u8),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LogicOp { And, Ora, Eor }

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ArithmeticOp { Adc, Sbc }

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ModifyOp { Inc, Dec, Asl, Lsr, Rol, Ror }

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ModifyTarget { Accumulator, Memory(Operand) }

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StackValue { A, Status }

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum IrOp {
    SetFlag { flag: Flag, value: bool },
    Load { dst: Register, src: Operand },
    Store { src: Register, dst: Operand },
    Transfer { src: Register, dst: Register, update_nz: bool },
    Inc(Register),
    Dec(Register),
    Logic { op: LogicOp, rhs: Operand },
    Arithmetic { op: ArithmeticOp, rhs: Operand },
    Modify { op: ModifyOp, target: ModifyTarget },
    Bit { rhs: Operand },
    Compare { reg: Register, rhs: Operand },
    StackPush(StackValue),
    StackPop(StackValue),
    Branch { flag: Flag, when: bool, target: u16 },
    Jump(u16),
    JumpIndirect { pointer: u16 },
    Call { target: u16, return_addr: u16 },
    Return,
    ReturnInterrupt,
    Break { return_pc: u16 },
    ReadIo { addr: u16, dst: Register },
    WriteIo { addr: u16, src: Register },
    Nop,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LowerError {
    Unsupported { pc: u16, mnemonic: Mnemonic, mode: AddressingMode },
}

impl fmt::Display for LowerError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Unsupported { pc, mnemonic, mode } =>
                write!(f, "unsupported IR lowering at ${pc:04X}: {mnemonic:?} {mode:?}"),
        }
    }
}
impl std::error::Error for LowerError {}

fn operand(i: DecodedInstruction) -> Option<Operand> {
    match i.def.mode {
        AddressingMode::Immediate => Some(Operand::Immediate(i.operand as u8)),
        AddressingMode::ZeroPage => Some(Operand::ZeroPage(i.operand as u8)),
        AddressingMode::ZeroPageX => Some(Operand::ZeroPageX(i.operand as u8)),
        AddressingMode::ZeroPageY => Some(Operand::ZeroPageY(i.operand as u8)),
        AddressingMode::Absolute => Some(Operand::Absolute(i.operand)),
        AddressingMode::AbsoluteX => Some(Operand::AbsoluteX(i.operand)),
        AddressingMode::AbsoluteY => Some(Operand::AbsoluteY(i.operand)),
        AddressingMode::IndexedIndirect => Some(Operand::IndexedIndirect(i.operand as u8)),
        AddressingMode::IndirectIndexed => Some(Operand::IndirectIndexed(i.operand as u8)),
        _ => None,
    }
}

fn relative_target(i: DecodedInstruction) -> u16 {
    let next = i.pc.wrapping_add(i.def.len() as u16);
    next.wrapping_add((i.operand as u8 as i8) as i16 as u16)
}

fn is_io_absolute(i: DecodedInstruction) -> bool {
    i.def.mode == AddressingMode::Absolute && (0x2000..=0x4017).contains(&i.operand)
}

pub fn lower_instruction(i: DecodedInstruction) -> Result<Vec<IrOp>, LowerError> {
    use Flag::*;
    use IrOp::*;
    use Register::*;

    let unsupported = || LowerError::Unsupported { pc: i.pc, mnemonic: i.def.mnemonic, mode: i.def.mode };
    let op = || operand(i).ok_or_else(unsupported);

    let ops = match (i.def.mnemonic, i.def.mode) {
        (Mnemonic::Cld, AddressingMode::Implied) => vec![SetFlag { flag: Decimal, value: false }],
        (Mnemonic::Sed, AddressingMode::Implied) => vec![SetFlag { flag: Decimal, value: true }],
        (Mnemonic::Sei, AddressingMode::Implied) => vec![SetFlag { flag: InterruptDisable, value: true }],
        (Mnemonic::Cli, AddressingMode::Implied) => vec![SetFlag { flag: InterruptDisable, value: false }],
        (Mnemonic::Clc, AddressingMode::Implied) => vec![SetFlag { flag: Carry, value: false }],
        (Mnemonic::Sec, AddressingMode::Implied) => vec![SetFlag { flag: Carry, value: true }],
        (Mnemonic::Clv, AddressingMode::Implied) => vec![SetFlag { flag: Overflow, value: false }],

        (Mnemonic::Lda, AddressingMode::Absolute) if is_io_absolute(i) => vec![ReadIo { addr: i.operand, dst: A }],
        (Mnemonic::Lda, _) => vec![Load { dst: A, src: op()? }],
        (Mnemonic::Ldx, _) => vec![Load { dst: X, src: op()? }],
        (Mnemonic::Ldy, _) => vec![Load { dst: Y, src: op()? }],

        (Mnemonic::Sta, AddressingMode::Absolute) if is_io_absolute(i) => vec![WriteIo { addr: i.operand, src: A }],
        (Mnemonic::Stx, AddressingMode::Absolute) if is_io_absolute(i) => vec![WriteIo { addr: i.operand, src: X }],
        (Mnemonic::Sty, AddressingMode::Absolute) if is_io_absolute(i) => vec![WriteIo { addr: i.operand, src: Y }],
        (Mnemonic::Sta, _) => vec![Store { src: A, dst: op()? }],
        (Mnemonic::Stx, _) => vec![Store { src: X, dst: op()? }],
        (Mnemonic::Sty, _) => vec![Store { src: Y, dst: op()? }],

        (Mnemonic::Inx, AddressingMode::Implied) => vec![Inc(X)],
        (Mnemonic::Iny, AddressingMode::Implied) => vec![Inc(Y)],
        (Mnemonic::Dex, AddressingMode::Implied) => vec![Dec(X)],
        (Mnemonic::Dey, AddressingMode::Implied) => vec![Dec(Y)],

        (Mnemonic::Txs, AddressingMode::Implied) => vec![Transfer { src: X, dst: Sp, update_nz: false }],
        (Mnemonic::Tsx, AddressingMode::Implied) => vec![Transfer { src: Sp, dst: X, update_nz: true }],
        (Mnemonic::Txa, AddressingMode::Implied) => vec![Transfer { src: X, dst: A, update_nz: true }],
        (Mnemonic::Tya, AddressingMode::Implied) => vec![Transfer { src: Y, dst: A, update_nz: true }],
        (Mnemonic::Tax, AddressingMode::Implied) => vec![Transfer { src: A, dst: X, update_nz: true }],
        (Mnemonic::Tay, AddressingMode::Implied) => vec![Transfer { src: A, dst: Y, update_nz: true }],

        (Mnemonic::And, _) => vec![Logic { op: LogicOp::And, rhs: op()? }],
        (Mnemonic::Ora, _) => vec![Logic { op: LogicOp::Ora, rhs: op()? }],
        (Mnemonic::Eor, _) => vec![Logic { op: LogicOp::Eor, rhs: op()? }],
        (Mnemonic::Adc, _) => vec![Arithmetic { op: ArithmeticOp::Adc, rhs: op()? }],
        (Mnemonic::Sbc, _) => vec![Arithmetic { op: ArithmeticOp::Sbc, rhs: op()? }],

        (Mnemonic::Inc, _) => vec![Modify { op: ModifyOp::Inc, target: ModifyTarget::Memory(op()?) }],
        (Mnemonic::Dec, _) => vec![Modify { op: ModifyOp::Dec, target: ModifyTarget::Memory(op()?) }],
        (Mnemonic::Asl, AddressingMode::Accumulator) => vec![Modify { op: ModifyOp::Asl, target: ModifyTarget::Accumulator }],
        (Mnemonic::Lsr, AddressingMode::Accumulator) => vec![Modify { op: ModifyOp::Lsr, target: ModifyTarget::Accumulator }],
        (Mnemonic::Rol, AddressingMode::Accumulator) => vec![Modify { op: ModifyOp::Rol, target: ModifyTarget::Accumulator }],
        (Mnemonic::Ror, AddressingMode::Accumulator) => vec![Modify { op: ModifyOp::Ror, target: ModifyTarget::Accumulator }],
        (Mnemonic::Asl, _) => vec![Modify { op: ModifyOp::Asl, target: ModifyTarget::Memory(op()?) }],
        (Mnemonic::Lsr, _) => vec![Modify { op: ModifyOp::Lsr, target: ModifyTarget::Memory(op()?) }],
        (Mnemonic::Rol, _) => vec![Modify { op: ModifyOp::Rol, target: ModifyTarget::Memory(op()?) }],
        (Mnemonic::Ror, _) => vec![Modify { op: ModifyOp::Ror, target: ModifyTarget::Memory(op()?) }],
        (Mnemonic::Bit, _) => vec![IrOp::Bit { rhs: op()? }],

        (Mnemonic::Cmp, _) => vec![Compare { reg: A, rhs: op()? }],
        (Mnemonic::Cpx, _) => vec![Compare { reg: X, rhs: op()? }],
        (Mnemonic::Cpy, _) => vec![Compare { reg: Y, rhs: op()? }],

        (Mnemonic::Pha, AddressingMode::Implied) => vec![StackPush(StackValue::A)],
        (Mnemonic::Php, AddressingMode::Implied) => vec![StackPush(StackValue::Status)],
        (Mnemonic::Pla, AddressingMode::Implied) => vec![StackPop(StackValue::A)],
        (Mnemonic::Plp, AddressingMode::Implied) => vec![StackPop(StackValue::Status)],

        (Mnemonic::Bpl, AddressingMode::Relative) => vec![Branch { flag: Negative, when: false, target: relative_target(i) }],
        (Mnemonic::Bmi, AddressingMode::Relative) => vec![Branch { flag: Negative, when: true, target: relative_target(i) }],
        (Mnemonic::Beq, AddressingMode::Relative) => vec![Branch { flag: Zero, when: true, target: relative_target(i) }],
        (Mnemonic::Bne, AddressingMode::Relative) => vec![Branch { flag: Zero, when: false, target: relative_target(i) }],
        (Mnemonic::Bcc, AddressingMode::Relative) => vec![Branch { flag: Carry, when: false, target: relative_target(i) }],
        (Mnemonic::Bcs, AddressingMode::Relative) => vec![Branch { flag: Carry, when: true, target: relative_target(i) }],
        (Mnemonic::Bvc, AddressingMode::Relative) => vec![Branch { flag: Overflow, when: false, target: relative_target(i) }],
        (Mnemonic::Bvs, AddressingMode::Relative) => vec![Branch { flag: Overflow, when: true, target: relative_target(i) }],

        (Mnemonic::Jmp, AddressingMode::Absolute) => vec![Jump(i.operand)],
        (Mnemonic::Jmp, AddressingMode::Indirect) => vec![JumpIndirect { pointer: i.operand }],
        (Mnemonic::Jsr, AddressingMode::Absolute) => vec![Call {
            target: i.operand,
            return_addr: i.pc.wrapping_add(2),
        }],
        (Mnemonic::Rts, AddressingMode::Implied) => vec![Return],
        (Mnemonic::Rti, AddressingMode::Implied) => vec![ReturnInterrupt],
        (Mnemonic::Brk, AddressingMode::Implied) => vec![Break { return_pc: i.pc.wrapping_add(2) }],
        (Mnemonic::Nop, AddressingMode::Implied) => vec![Nop],
        _ => return Err(unsupported()),
    };
    Ok(ops)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cpu6502;

    #[test]
    fn reset_path_io_read_lowers_semantically() {
        let i = cpu6502::decode(0xEDB9, &[0xAD, 0x02, 0x20]).unwrap();
        assert_eq!(lower_instruction(i).unwrap(), vec![IrOp::ReadIo { addr: 0x2002, dst: Register::A }]);
    }

    #[test]
    fn indexed_reset_clear_loop_is_supported() {
        let i = cpu6502::decode(0xEDEB, &[0x95, 0x00]).unwrap();
        assert_eq!(lower_instruction(i).unwrap(),
            vec![IrOp::Store { src: Register::A, dst: Operand::ZeroPageX(0) }]);
    }

    #[test]
    fn jsr_records_6502_return_address() {
        let i = cpu6502::decode(0xEDCC, &[0x20, 0xFE, 0xEF]).unwrap();
        assert_eq!(lower_instruction(i).unwrap(),
            vec![IrOp::Call { target: 0xEFFE, return_addr: 0xEDCE }]);
    }
}
