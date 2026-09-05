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
    Compare { reg: Register, rhs: Operand },
    StackPush(StackValue),
    StackPop(StackValue),
    Branch { flag: Flag, when: bool, target: u16 },
    Jump(u16),
    Call { target: u16, return_addr: u16 },
    Return,
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
    use Mnemonic::*;
    use Register::*;

    let unsupported = || LowerError::Unsupported { pc: i.pc, mnemonic: i.def.mnemonic, mode: i.def.mode };
    let op = || operand(i).ok_or_else(unsupported);

    let ops = match (i.def.mnemonic, i.def.mode) {
        (Cld, AddressingMode::Implied) => vec![SetFlag { flag: Decimal, value: false }],
        (Sed, AddressingMode::Implied) => vec![SetFlag { flag: Decimal, value: true }],
        (Sei, AddressingMode::Implied) => vec![SetFlag { flag: InterruptDisable, value: true }],
        (Cli, AddressingMode::Implied) => vec![SetFlag { flag: InterruptDisable, value: false }],
        (Clc, AddressingMode::Implied) => vec![SetFlag { flag: Carry, value: false }],
        (Sec, AddressingMode::Implied) => vec![SetFlag { flag: Carry, value: true }],
        (Clv, AddressingMode::Implied) => vec![SetFlag { flag: Overflow, value: false }],

        (Lda, AddressingMode::Absolute) if is_io_absolute(i) => vec![ReadIo { addr: i.operand, dst: A }],
        (Lda, _) => vec![Load { dst: A, src: op()? }],
        (Ldx, _) => vec![Load { dst: X, src: op()? }],
        (Ldy, _) => vec![Load { dst: Y, src: op()? }],

        (Sta, AddressingMode::Absolute) if is_io_absolute(i) => vec![WriteIo { addr: i.operand, src: A }],
        (Stx, AddressingMode::Absolute) if is_io_absolute(i) => vec![WriteIo { addr: i.operand, src: X }],
        (Sty, AddressingMode::Absolute) if is_io_absolute(i) => vec![WriteIo { addr: i.operand, src: Y }],
        (Sta, _) => vec![Store { src: A, dst: op()? }],
        (Stx, _) => vec![Store { src: X, dst: op()? }],
        (Sty, _) => vec![Store { src: Y, dst: op()? }],

        (Inx, AddressingMode::Implied) => vec![Inc(X)],
        (Iny, AddressingMode::Implied) => vec![Inc(Y)],
        (Dex, AddressingMode::Implied) => vec![Dec(X)],
        (Dey, AddressingMode::Implied) => vec![Dec(Y)],

        (Txs, AddressingMode::Implied) => vec![Transfer { src: X, dst: Sp, update_nz: false }],
        (Tsx, AddressingMode::Implied) => vec![Transfer { src: Sp, dst: X, update_nz: true }],
        (Txa, AddressingMode::Implied) => vec![Transfer { src: X, dst: A, update_nz: true }],
        (Tya, AddressingMode::Implied) => vec![Transfer { src: Y, dst: A, update_nz: true }],
        (Tax, AddressingMode::Implied) => vec![Transfer { src: A, dst: X, update_nz: true }],
        (Tay, AddressingMode::Implied) => vec![Transfer { src: A, dst: Y, update_nz: true }],

        (And, _) => vec![Logic { op: LogicOp::And, rhs: op()? }],
        (Ora, _) => vec![Logic { op: LogicOp::Ora, rhs: op()? }],
        (Eor, _) => vec![Logic { op: LogicOp::Eor, rhs: op()? }],

        (Cmp, _) => vec![Compare { reg: A, rhs: op()? }],
        (Cpx, _) => vec![Compare { reg: X, rhs: op()? }],
        (Cpy, _) => vec![Compare { reg: Y, rhs: op()? }],

        (Pha, AddressingMode::Implied) => vec![StackPush(StackValue::A)],
        (Php, AddressingMode::Implied) => vec![StackPush(StackValue::Status)],
        (Pla, AddressingMode::Implied) => vec![StackPop(StackValue::A)],
        (Plp, AddressingMode::Implied) => vec![StackPop(StackValue::Status)],

        (Bpl, AddressingMode::Relative) => vec![Branch { flag: Negative, when: false, target: relative_target(i) }],
        (Bmi, AddressingMode::Relative) => vec![Branch { flag: Negative, when: true, target: relative_target(i) }],
        (Beq, AddressingMode::Relative) => vec![Branch { flag: Zero, when: true, target: relative_target(i) }],
        (Bne, AddressingMode::Relative) => vec![Branch { flag: Zero, when: false, target: relative_target(i) }],
        (Bcc, AddressingMode::Relative) => vec![Branch { flag: Carry, when: false, target: relative_target(i) }],
        (Bcs, AddressingMode::Relative) => vec![Branch { flag: Carry, when: true, target: relative_target(i) }],
        (Bvc, AddressingMode::Relative) => vec![Branch { flag: Overflow, when: false, target: relative_target(i) }],
        (Bvs, AddressingMode::Relative) => vec![Branch { flag: Overflow, when: true, target: relative_target(i) }],

        (Jmp, AddressingMode::Absolute) => vec![Jump(i.operand)],
        (Jsr, AddressingMode::Absolute) => vec![Call {
            target: i.operand,
            return_addr: i.pc.wrapping_add(2),
        }],
        (Rts, AddressingMode::Implied) => vec![Return],
        (Nop, AddressingMode::Implied) => vec![Nop],
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
