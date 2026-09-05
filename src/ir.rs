use std::fmt;

use crate::cpu6502::{AddressingMode, DecodedInstruction, Mnemonic};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Register { A, X, Y, Sp }

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Flag { Carry, Zero, InterruptDisable, Decimal, Overflow, Negative }

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Operand { Immediate(u8), ZeroPage(u8), Absolute(u16) }

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum IrOp {
    SetFlag { flag: Flag, value: bool },
    Load { dst: Register, src: Operand },
    Store { src: Register, dst: Operand },
    Transfer { src: Register, dst: Register },
    Dec(Register),
    Compare { reg: Register, rhs: Operand },
    Branch { flag: Flag, when: bool, target: u16 },
    Jump(u16),
    Call(u16),
    Return,
    ReadIo { addr: u16, dst: Register },
    WriteIo { addr: u16, src: Register },
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
        AddressingMode::Absolute => Some(Operand::Absolute(i.operand)),
        _ => None,
    }
}

fn relative_target(i: DecodedInstruction) -> u16 {
    let next = i.pc.wrapping_add(i.def.len() as u16);
    next.wrapping_add((i.operand as u8 as i8) as i16 as u16)
}

pub fn lower_instruction(i: DecodedInstruction) -> Result<Vec<IrOp>, LowerError> {
    use Flag::*;
    use IrOp::*;
    use Mnemonic::*;
    use Register::*;

    let unsupported = || LowerError::Unsupported { pc: i.pc, mnemonic: i.def.mnemonic, mode: i.def.mode };
    let ops = match (i.def.mnemonic, i.def.mode) {
        (Cld, AddressingMode::Implied) => vec![SetFlag { flag: Decimal, value: false }],
        (Sei, AddressingMode::Implied) => vec![SetFlag { flag: InterruptDisable, value: true }],
        (Cli, AddressingMode::Implied) => vec![SetFlag { flag: InterruptDisable, value: false }],
        (Clc, AddressingMode::Implied) => vec![SetFlag { flag: Carry, value: false }],
        (Sec, AddressingMode::Implied) => vec![SetFlag { flag: Carry, value: true }],
        (Clv, AddressingMode::Implied) => vec![SetFlag { flag: Overflow, value: false }],

        (Lda, AddressingMode::Absolute) if (0x2000..=0x4017).contains(&i.operand) =>
            vec![ReadIo { addr: i.operand, dst: A }],
        (Lda, _) => vec![Load { dst: A, src: operand(i).ok_or_else(unsupported)? }],
        (Ldx, _) => vec![Load { dst: X, src: operand(i).ok_or_else(unsupported)? }],
        (Ldy, _) => vec![Load { dst: Y, src: operand(i).ok_or_else(unsupported)? }],

        (Sta, AddressingMode::Absolute) if (0x2000..=0x4017).contains(&i.operand) =>
            vec![WriteIo { addr: i.operand, src: A }],
        (Stx, AddressingMode::Absolute) if (0x2000..=0x4017).contains(&i.operand) =>
            vec![WriteIo { addr: i.operand, src: X }],
        (Sty, AddressingMode::Absolute) if (0x2000..=0x4017).contains(&i.operand) =>
            vec![WriteIo { addr: i.operand, src: Y }],
        (Sta, _) => vec![Store { src: A, dst: operand(i).ok_or_else(unsupported)? }],
        (Stx, _) => vec![Store { src: X, dst: operand(i).ok_or_else(unsupported)? }],
        (Sty, _) => vec![Store { src: Y, dst: operand(i).ok_or_else(unsupported)? }],

        (Dex, AddressingMode::Implied) => vec![Dec(X)],
        (Dey, AddressingMode::Implied) => vec![Dec(Y)],
        (Txs, AddressingMode::Implied) => vec![Transfer { src: X, dst: Sp }],
        (Txa, AddressingMode::Implied) => vec![Transfer { src: X, dst: A }],
        (Tya, AddressingMode::Implied) => vec![Transfer { src: Y, dst: A }],
        (Tax, AddressingMode::Implied) => vec![Transfer { src: A, dst: X }],
        (Tay, AddressingMode::Implied) => vec![Transfer { src: A, dst: Y }],

        (Cmp, _) => vec![Compare { reg: A, rhs: operand(i).ok_or_else(unsupported)? }],
        (Cpx, _) => vec![Compare { reg: X, rhs: operand(i).ok_or_else(unsupported)? }],
        (Cpy, _) => vec![Compare { reg: Y, rhs: operand(i).ok_or_else(unsupported)? }],

        (Bpl, AddressingMode::Relative) => vec![Branch { flag: Negative, when: false, target: relative_target(i) }],
        (Bmi, AddressingMode::Relative) => vec![Branch { flag: Negative, when: true, target: relative_target(i) }],
        (Beq, AddressingMode::Relative) => vec![Branch { flag: Zero, when: true, target: relative_target(i) }],
        (Bne, AddressingMode::Relative) => vec![Branch { flag: Zero, when: false, target: relative_target(i) }],
        (Bcc, AddressingMode::Relative) => vec![Branch { flag: Carry, when: false, target: relative_target(i) }],
        (Bcs, AddressingMode::Relative) => vec![Branch { flag: Carry, when: true, target: relative_target(i) }],
        (Bvc, AddressingMode::Relative) => vec![Branch { flag: Overflow, when: false, target: relative_target(i) }],
        (Bvs, AddressingMode::Relative) => vec![Branch { flag: Overflow, when: true, target: relative_target(i) }],

        (Jmp, AddressingMode::Absolute) => vec![Jump(i.operand)],
        (Jsr, AddressingMode::Absolute) => vec![Call(i.operand)],
        (Rts, AddressingMode::Implied) => vec![Return],
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
    fn bpl_target_is_absolute() {
        let i = cpu6502::decode(0xEDBC, &[0x10, 0xFB]).unwrap();
        assert_eq!(lower_instruction(i).unwrap(),
            vec![IrOp::Branch { flag: Flag::Negative, when: false, target: 0xEDB9 }]);
    }
}
