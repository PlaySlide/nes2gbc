use std::fmt;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mnemonic {
    Adc, And, Asl, Bcc, Bcs, Beq, Bit, Bmi, Bne, Bpl, Brk, Bvc, Bvs,
    Clc, Cld, Cli, Clv, Cmp, Cpx, Cpy, Dec, Dex, Dey, Eor, Inc, Inx, Iny,
    Jmp, Jsr, Lda, Ldx, Ldy, Lsr, Nop, Ora, Pha, Php, Pla, Plp, Rol, Ror,
    Rti, Rts, Sbc, Sec, Sed, Sei, Sta, Stx, Sty, Tax, Tay, Tsx, Txa, Txs, Tya,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AddressingMode { Implied, Accumulator, Immediate, ZeroPage, ZeroPageX, ZeroPageY, Relative, Absolute, AbsoluteX, AbsoluteY, Indirect, IndexedIndirect, IndirectIndexed }
impl AddressingMode {
    pub const fn len(self) -> u8 { match self {
        Self::Implied|Self::Accumulator => 1,
        Self::Immediate|Self::ZeroPage|Self::ZeroPageX|Self::ZeroPageY|Self::Relative|Self::IndexedIndirect|Self::IndirectIndexed => 2,
        _ => 3,
    }}
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct InstructionDef { pub mnemonic: Mnemonic, pub mode: AddressingMode }
impl InstructionDef { pub const fn len(self)->u8 { self.mode.len() } }
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DecodedInstruction { pub pc:u16, pub opcode:u8, pub def:InstructionDef, pub operand:u16 }
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DecodeError { IllegalOpcode{pc:u16,opcode:u8}, Truncated{pc:u16,needed:u8,available:usize} }
impl fmt::Display for DecodeError {
    fn fmt(&self,f:&mut fmt::Formatter<'_>)->fmt::Result { match *self {
        Self::IllegalOpcode{pc,opcode}=>write!(f,"illegal/unimplemented 6502 opcode ${opcode:02X} at ${pc:04X}"),
        Self::Truncated{pc,needed,available}=>write!(f,"truncated instruction at ${pc:04X}: needs {needed} byte(s), only {available} available"),
    }}
}
impl std::error::Error for DecodeError {}
macro_rules! op { ($m:ident,$a:ident)=>{Some(InstructionDef{mnemonic:Mnemonic::$m,mode:AddressingMode::$a})}; }
pub const fn opcode_def(o:u8)->Option<InstructionDef>{match o{
0x00=>op!(Brk,Implied),0x01=>op!(Ora,IndexedIndirect),0x05=>op!(Ora,ZeroPage),0x06=>op!(Asl,ZeroPage),0x08=>op!(Php,Implied),0x09=>op!(Ora,Immediate),0x0A=>op!(Asl,Accumulator),0x0D=>op!(Ora,Absolute),0x0E=>op!(Asl,Absolute),
0x10=>op!(Bpl,Relative),0x11=>op!(Ora,IndirectIndexed),0x15=>op!(Ora,ZeroPageX),0x16=>op!(Asl,ZeroPageX),0x18=>op!(Clc,Implied),0x19=>op!(Ora,AbsoluteY),0x1D=>op!(Ora,AbsoluteX),0x1E=>op!(Asl,AbsoluteX),
0x20=>op!(Jsr,Absolute),0x21=>op!(And,IndexedIndirect),0x24=>op!(Bit,ZeroPage),0x25=>op!(And,ZeroPage),0x26=>op!(Rol,ZeroPage),0x28=>op!(Plp,Implied),0x29=>op!(And,Immediate),0x2A=>op!(Rol,Accumulator),0x2C=>op!(Bit,Absolute),0x2D=>op!(And,Absolute),0x2E=>op!(Rol,Absolute),
0x30=>op!(Bmi,Relative),0x31=>op!(And,IndirectIndexed),0x35=>op!(And,ZeroPageX),0x36=>op!(Rol,ZeroPageX),0x38=>op!(Sec,Implied),0x39=>op!(And,AbsoluteY),0x3D=>op!(And,AbsoluteX),0x3E=>op!(Rol,AbsoluteX),
0x40=>op!(Rti,Implied),0x41=>op!(Eor,IndexedIndirect),0x45=>op!(Eor,ZeroPage),0x46=>op!(Lsr,ZeroPage),0x48=>op!(Pha,Implied),0x49=>op!(Eor,Immediate),0x4A=>op!(Lsr,Accumulator),0x4C=>op!(Jmp,Absolute),0x4D=>op!(Eor,Absolute),0x4E=>op!(Lsr,Absolute),
0x50=>op!(Bvc,Relative),0x51=>op!(Eor,IndirectIndexed),0x55=>op!(Eor,ZeroPageX),0x56=>op!(Lsr,ZeroPageX),0x58=>op!(Cli,Implied),0x59=>op!(Eor,AbsoluteY),0x5D=>op!(Eor,AbsoluteX),0x5E=>op!(Lsr,AbsoluteX),
0x60=>op!(Rts,Implied),0x61=>op!(Adc,IndexedIndirect),0x65=>op!(Adc,ZeroPage),0x66=>op!(Ror,ZeroPage),0x68=>op!(Pla,Implied),0x69=>op!(Adc,Immediate),0x6A=>op!(Ror,Accumulator),0x6C=>op!(Jmp,Indirect),0x6D=>op!(Adc,Absolute),0x6E=>op!(Ror,Absolute),
0x70=>op!(Bvs,Relative),0x71=>op!(Adc,IndirectIndexed),0x75=>op!(Adc,ZeroPageX),0x76=>op!(Ror,ZeroPageX),0x78=>op!(Sei,Implied),0x79=>op!(Adc,AbsoluteY),0x7D=>op!(Adc,AbsoluteX),0x7E=>op!(Ror,AbsoluteX),
0x81=>op!(Sta,IndexedIndirect),0x84=>op!(Sty,ZeroPage),0x85=>op!(Sta,ZeroPage),0x86=>op!(Stx,ZeroPage),0x88=>op!(Dey,Implied),0x8A=>op!(Txa,Implied),0x8C=>op!(Sty,Absolute),0x8D=>op!(Sta,Absolute),0x8E=>op!(Stx,Absolute),
0x90=>op!(Bcc,Relative),0x91=>op!(Sta,IndirectIndexed),0x94=>op!(Sty,ZeroPageX),0x95=>op!(Sta,ZeroPageX),0x96=>op!(Stx,ZeroPageY),0x98=>op!(Tya,Implied),0x99=>op!(Sta,AbsoluteY),0x9A=>op!(Txs,Implied),0x9D=>op!(Sta,AbsoluteX),
0xA0=>op!(Ldy,Immediate),0xA1=>op!(Lda,IndexedIndirect),0xA2=>op!(Ldx,Immediate),0xA4=>op!(Ldy,ZeroPage),0xA5=>op!(Lda,ZeroPage),0xA6=>op!(Ldx,ZeroPage),0xA8=>op!(Tay,Implied),0xA9=>op!(Lda,Immediate),0xAA=>op!(Tax,Implied),0xAC=>op!(Ldy,Absolute),0xAD=>op!(Lda,Absolute),0xAE=>op!(Ldx,Absolute),
0xB0=>op!(Bcs,Relative),0xB1=>op!(Lda,IndirectIndexed),0xB4=>op!(Ldy,ZeroPageX),0xB5=>op!(Lda,ZeroPageX),0xB6=>op!(Ldx,ZeroPageY),0xB8=>op!(Clv,Implied),0xB9=>op!(Lda,AbsoluteY),0xBA=>op!(Tsx,Implied),0xBC=>op!(Ldy,AbsoluteX),0xBD=>op!(Lda,AbsoluteX),0xBE=>op!(Ldx,AbsoluteY),
0xC0=>op!(Cpy,Immediate),0xC1=>op!(Cmp,IndexedIndirect),0xC4=>op!(Cpy,ZeroPage),0xC5=>op!(Cmp,ZeroPage),0xC6=>op!(Dec,ZeroPage),0xC8=>op!(Iny,Implied),0xC9=>op!(Cmp,Immediate),0xCA=>op!(Dex,Implied),0xCC=>op!(Cpy,Absolute),0xCD=>op!(Cmp,Absolute),0xCE=>op!(Dec,Absolute),
0xD0=>op!(Bne,Relative),0xD1=>op!(Cmp,IndirectIndexed),0xD5=>op!(Cmp,ZeroPageX),0xD6=>op!(Dec,ZeroPageX),0xD8=>op!(Cld,Implied),0xD9=>op!(Cmp,AbsoluteY),0xDD=>op!(Cmp,AbsoluteX),0xDE=>op!(Dec,AbsoluteX),
0xE0=>op!(Cpx,Immediate),0xE1=>op!(Sbc,IndexedIndirect),0xE4=>op!(Cpx,ZeroPage),0xE5=>op!(Sbc,ZeroPage),0xE6=>op!(Inc,ZeroPage),0xE8=>op!(Inx,Implied),0xE9=>op!(Sbc,Immediate),0xEA=>op!(Nop,Implied),0xEC=>op!(Cpx,Absolute),0xED=>op!(Sbc,Absolute),0xEE=>op!(Inc,Absolute),
0xF0=>op!(Beq,Relative),0xF1=>op!(Sbc,IndirectIndexed),0xF5=>op!(Sbc,ZeroPageX),0xF6=>op!(Inc,ZeroPageX),0xF8=>op!(Sed,Implied),0xF9=>op!(Sbc,AbsoluteY),0xFD=>op!(Sbc,AbsoluteX),0xFE=>op!(Inc,AbsoluteX),
_=>None}}
pub fn decode(pc:u16,b:&[u8])->Result<DecodedInstruction,DecodeError>{
    let Some(&opcode)=b.first() else{return Err(DecodeError::Truncated{pc,needed:1,available:0})};
    let Some(def)=opcode_def(opcode) else{return Err(DecodeError::IllegalOpcode{pc,opcode})};
    let len=def.len(); if b.len()<len as usize{return Err(DecodeError::Truncated{pc,needed:len,available:b.len()})}
    let operand=match len{1=>0,2=>b[1] as u16,3=>u16::from_le_bytes([b[1],b[2]]),_=>unreachable!()};
    Ok(DecodedInstruction{pc,opcode,def,operand})
}
#[derive(Debug,Clone,Copy,PartialEq,Eq)]
pub struct Vectors{pub nmi:u16,pub reset:u16,pub irq_brk:u16}
pub fn vectors_from_prg(prg:&[u8])->Option<Vectors>{if prg.len()<6{return None}let v=&prg[prg.len()-6..];Some(Vectors{nmi:u16::from_le_bytes([v[0],v[1]]),reset:u16::from_le_bytes([v[2],v[3]]),irq_brk:u16::from_le_bytes([v[4],v[5]])})}
#[cfg(test)] mod tests{use super::*;#[test]fn official_count(){assert_eq!((0u16..=255).filter(|&x|opcode_def(x as u8).is_some()).count(),151)}#[test]fn lda(){let i=decode(0x8000,&[0xA9,0x42]).unwrap();assert_eq!(i.def.mnemonic,Mnemonic::Lda);assert_eq!(i.operand,0x42)}}
