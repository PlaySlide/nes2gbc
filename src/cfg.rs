use std::collections::{BTreeMap,BTreeSet,VecDeque};
use std::fmt;
use crate::cpu6502::{self,AddressingMode,DecodeError,DecodedInstruction,Mnemonic,Vectors};
#[derive(Debug,Clone,Copy,PartialEq,Eq)]pub enum EdgeKind{Fallthrough,BranchTaken,Jump,Call,CallReturn,IndirectJump{pointer:u16}}
#[derive(Debug,Clone,Copy,PartialEq,Eq)]pub struct Edge{pub kind:EdgeKind,pub target:Option<u16>}
#[derive(Debug,Clone,PartialEq,Eq)]pub struct BasicBlock{pub start:u16,pub instructions:Vec<DecodedInstruction>,pub edges:Vec<Edge>}
#[derive(Debug,Clone,PartialEq,Eq)]pub struct AnalysisDiagnostic{pub pc:u16,pub error:DecodeError}
#[derive(Debug,Clone,PartialEq,Eq)]pub struct ControlFlowGraph{pub blocks:BTreeMap<u16,BasicBlock>,pub entry_points:Vec<u16>,pub diagnostics:Vec<AnalysisDiagnostic>}
#[derive(Debug,Clone,PartialEq,Eq)]pub enum AnalysisError{UnsupportedMapper(u16),UnsupportedPrgSize(usize),UnmappedAddress(u16),Decode(DecodeError)}
impl fmt::Display for AnalysisError{fn fmt(&self,f:&mut fmt::Formatter<'_>)->fmt::Result{match self{
Self::UnsupportedMapper(m)=>write!(f,"CFG discovery currently supports mapper 0 and 3, not mapper {m}"),
Self::UnsupportedPrgSize(n)=>write!(f,"CFG discovery currently expects 16 KiB or 32 KiB fixed PRG, got {} KiB",n/1024),
Self::UnmappedAddress(a)=>write!(f,"CPU address ${a:04X} is outside fixed PRG ROM"),Self::Decode(e)=>e.fmt(f)}}}
impl std::error::Error for AnalysisError{} impl From<DecodeError> for AnalysisError{fn from(v:DecodeError)->Self{Self::Decode(v)}}
fn off(mapper:u16,len:usize,a:u16)->Result<usize,AnalysisError>{if mapper!=0&&mapper!=3{return Err(AnalysisError::UnsupportedMapper(mapper))}if a<0x8000{return Err(AnalysisError::UnmappedAddress(a))}let x=(a-0x8000)as usize;match len{0x4000=>Ok(x&0x3fff),0x8000=>Ok(x),n=>Err(AnalysisError::UnsupportedPrgSize(n))}}
fn dec(mapper:u16,prg:&[u8],pc:u16)->Result<DecodedInstruction,AnalysisError>{let o=off(mapper,prg.len(),pc)?;Ok(cpu6502::decode(pc,&prg[o..])?)}
fn branch(m:Mnemonic)->bool{matches!(m,Mnemonic::Bcc|Mnemonic::Bcs|Mnemonic::Beq|Mnemonic::Bmi|Mnemonic::Bne|Mnemonic::Bpl|Mnemonic::Bvc|Mnemonic::Bvs)}
fn rel(i:DecodedInstruction)->u16{let n=i.pc.wrapping_add(i.def.len()as u16);n.wrapping_add((i.operand as u8 as i8)as i16 as u16)}
fn q(q:&mut VecDeque<u16>,seen:&mut BTreeSet<u16>,t:u16){if t>=0x8000&&seen.insert(t){q.push_back(t)}}
fn looks_like_code(mapper:u16,prg:&[u8],start:u16)->bool{
 let mut pc=start;let mut n=0usize;
 while n<8{
  let i=match dec(mapper,prg,pc){Ok(i)=>i,Err(_)=>return false};n+=1;
  if matches!(i.def.mnemonic,Mnemonic::Jmp|Mnemonic::Rts|Mnemonic::Rti|Mnemonic::Brk){return true}
  pc=pc.wrapping_add(i.def.len()as u16);
 }
 true
}

// Recognize the common 6502 jump-table idiom:
//   LDA table,Y / STA ptr / INY / LDA table,Y / STA ptr+1 / ... / JMP (ptr)
// The index is often derived from a small state nibble. Conservatively inspect the
// first 16 little-endian table entries and keep only destinations that decode as code.
fn indirect_table_targets(mapper:u16,prg:&[u8],jmp_pc:u16,pointer:u16)->Vec<u16>{
 if pointer>0x00FE{return Vec::new()}

 // Form 1: pointer construction immediately precedes JMP (ptr):
 //   LDA table,Y / STA ptr / INY / LDA table,Y / STA ptr+1 / JMP (ptr)
 let start=jmp_pc.saturating_sub(64).max(0x8000);
 let mut tables=Vec::new();
 let mut pc=start;
 while pc.saturating_add(11)<=jmp_pc{
  let o=match off(mapper,prg.len(),pc){Ok(o)=>o,Err(_)=>break};
  if o+11<=prg.len()
   &&prg[o]==0xB9&&prg[o+3]==0x85&&prg[o+4]==pointer as u8
   &&prg[o+5]==0xC8&&prg[o+6]==0xB9
   &&prg[o+9]==0x85&&prg[o+10]==pointer.wrapping_add(1)as u8
   &&prg[o+1]==prg[o+7]&&prg[o+2]==prg[o+8]
  {
   let base=u16::from_le_bytes([prg[o+1],prg[o+2]]);
   if !tables.contains(&base){tables.push(base)}
  }
  pc=pc.wrapping_add(1);
 }

 // Form 2: a tiny JMP (ptr) trampoline whose caller builds the pointer:
 //   LDA table,Y / STA ptr / LDA table+1,Y / STA ptr+1 / JSR trampoline
 //   trampoline: JMP (ptr)
 //
 // Balloon Fight uses exactly this for its game-state dispatcher. The index is
 // already scaled by two, so table/table+1 are the low/high bytes of adjacent
 // little-endian targets.
 if let Ok(jmp_off)=off(mapper,prg.len(),jmp_pc){
  let jsr_lo=jmp_pc as u8;
  let jsr_hi=(jmp_pc>>8)as u8;
  let mut o=0usize;
  while o+13<=prg.len(){
   if prg[o]==0xB9
    &&prg[o+3]==0x85&&prg[o+4]==pointer as u8
    &&prg[o+5]==0xB9
    &&prg[o+8]==0x85&&prg[o+9]==pointer.wrapping_add(1)as u8
    &&prg[o+10]==0x20&&prg[o+11]==jsr_lo&&prg[o+12]==jsr_hi
   {
    let base=u16::from_le_bytes([prg[o+1],prg[o+2]]);
    let high_base=u16::from_le_bytes([prg[o+6],prg[o+7]]);
    if high_base==base.wrapping_add(1)&&!tables.contains(&base){tables.push(base)}
   }
   o+=1;
  }
  let _=jmp_off;
 }

 let mut out=Vec::new();
 for base in tables{
  for i in 0..16u16{
   let a=base.wrapping_add(i*2);
   let Ok(o)=off(mapper,prg.len(),a)else{continue};
   if o+1>=prg.len(){continue}
   let target=u16::from_le_bytes([prg[o],prg[o+1]]);
   if target>=0x8000&&looks_like_code(mapper,prg,target)&&!out.contains(&target){out.push(target)}
  }
 }
 out
}
pub fn discover_from_vectors(mapper:u16,prg:&[u8],v:Vectors)->Result<ControlFlowGraph,AnalysisError>{discover(mapper,prg,&[v.reset,v.nmi,v.irq_brk])}
pub fn discover(mapper:u16,prg:&[u8],entries:&[u16])->Result<ControlFlowGraph,AnalysisError>{
 if mapper!=0&&mapper!=3{return Err(AnalysisError::UnsupportedMapper(mapper))}if prg.len()!=0x4000&&prg.len()!=0x8000{return Err(AnalysisError::UnsupportedPrgSize(prg.len()))}
 let mut work=VecDeque::new();let mut seen=BTreeSet::new();for &e in entries{q(&mut work,&mut seen,e)}
 let mut blocks=BTreeMap::new();let mut diagnostics=Vec::new();
 while let Some(start)=work.pop_front(){if blocks.contains_key(&start){continue}let mut pc=start;let mut ins=Vec::new();let mut edges=Vec::new();
  loop{
   if pc!=start&&(blocks.contains_key(&pc)||seen.contains(&pc)){edges.push(Edge{kind:EdgeKind::Fallthrough,target:Some(pc)});break}
   let i=match dec(mapper,prg,pc){Ok(i)=>i,Err(AnalysisError::Decode(error))=>{diagnostics.push(AnalysisDiagnostic{pc,error});break},Err(e)=>return Err(e)};
   let next=pc.wrapping_add(i.def.len()as u16);ins.push(i);
   if branch(i.def.mnemonic){let t=rel(i);edges.push(Edge{kind:EdgeKind::BranchTaken,target:Some(t)});edges.push(Edge{kind:EdgeKind::Fallthrough,target:Some(next)});q(&mut work,&mut seen,t);q(&mut work,&mut seen,next);break}
   match i.def.mnemonic{
    Mnemonic::Jsr=>{let t=i.operand;edges.push(Edge{kind:EdgeKind::Call,target:Some(t)});edges.push(Edge{kind:EdgeKind::CallReturn,target:Some(next)});q(&mut work,&mut seen,t);q(&mut work,&mut seen,next);break}
    Mnemonic::Jmp if i.def.mode==AddressingMode::Absolute=>{let t=i.operand;edges.push(Edge{kind:EdgeKind::Jump,target:Some(t)});q(&mut work,&mut seen,t);break}
    Mnemonic::Jmp if i.def.mode==AddressingMode::Indirect=>{
     let targets=indirect_table_targets(mapper,prg,i.pc,i.operand);
     if targets.is_empty(){edges.push(Edge{kind:EdgeKind::IndirectJump{pointer:i.operand},target:None});}
     else{for t in targets{edges.push(Edge{kind:EdgeKind::IndirectJump{pointer:i.operand},target:Some(t)});q(&mut work,&mut seen,t);}}
     break
    }
    Mnemonic::Rts|Mnemonic::Rti|Mnemonic::Brk=>break,_=>pc=next
   }
  } blocks.insert(start,BasicBlock{start,instructions:ins,edges});
 }
 let mut ep=entries.to_vec();ep.sort_unstable();ep.dedup();Ok(ControlFlowGraph{blocks,entry_points:ep,diagnostics})
}
#[cfg(test)]
mod tests {
    use super::*;

    fn put(prg: &mut [u8], addr: u16, bytes: &[u8]) {
        let offset = (addr - 0x8000) as usize;
        prg[offset..offset + bytes.len()].copy_from_slice(bytes);
    }

    #[test]
    fn branch_cfg() {
        let mut prg = vec![0xEA; 0x8000];
        put(&mut prg, 0x8000, &[0xA9, 0x00, 0xF0, 0x02, 0x60, 0xEA, 0x60]);

        let graph = discover(0, &prg, &[0x8000]).unwrap();

        assert!(graph.blocks.contains_key(&0x8004));
        assert!(graph.blocks.contains_key(&0x8006));
    }

    #[test]
    fn discovers_indirect_targets_built_in_trampoline_caller() {
        let mut prg = vec![0xEA; 0x8000];

        // Caller: Y already contains an even target-table index.
        put(
            &mut prg,
            0x9000,
            &[
                0xB9, 0x00, 0xA0, // LDA $A000,Y
                0x85, 0x25,       // STA $25
                0xB9, 0x01, 0xA0, // LDA $A001,Y
                0x85, 0x26,       // STA $26
                0x20, 0x00, 0x91, // JSR $9100
                0x60,
            ],
        );
        put(&mut prg, 0x9100, &[0x6C, 0x25, 0x00]); // JMP ($0025)

        put(&mut prg, 0xA000, &[0x00, 0x92, 0x10, 0x92, 0x00, 0x00]);
        put(&mut prg, 0x9200, &[0x60]);
        put(&mut prg, 0x9210, &[0x60]);

        let graph = discover(0, &prg, &[0x9000]).unwrap();

        assert!(graph.blocks.contains_key(&0x9200));
        assert!(graph.blocks.contains_key(&0x9210));
        let trampoline = graph.blocks.get(&0x9100).unwrap();
        assert!(trampoline.edges.iter().any(|edge| {
            matches!(edge.kind, EdgeKind::IndirectJump { pointer: 0x0025 })
                && edge.target == Some(0x9200)
        }));
        assert!(trampoline.edges.iter().any(|edge| {
            matches!(edge.kind, EdgeKind::IndirectJump { pointer: 0x0025 })
                && edge.target == Some(0x9210)
        }));
    }

    #[test]
    fn discovers_indexed_indirect_jump_table_targets() {
        let mut prg = vec![0xEA; 0x8000];

        put(
            &mut prg,
            0x9000,
            &[
                0xB9, 0x00, 0xA0, // LDA $A000,Y
                0x85, 0x02,       // STA $02
                0xC8,             // INY
                0xB9, 0x00, 0xA0, // LDA $A000,Y
                0x85, 0x03,       // STA $03
                0x6C, 0x02, 0x00, // JMP ($0002)
            ],
        );

        let mut table = [0u8; 32];
        table[0] = 0x00;
        table[1] = 0x91;
        put(&mut prg, 0xA000, &table);
        put(&mut prg, 0x9100, &[0x60]); // RTS

        let graph = discover(0, &prg, &[0x9000]).unwrap();

        assert!(graph.blocks.contains_key(&0x9100));
        let block = graph.blocks.get(&0x9000).unwrap();
        assert!(block.edges.iter().any(|edge| {
            matches!(
                edge.kind,
                EdgeKind::IndirectJump { pointer: 0x0002 }
            ) && edge.target == Some(0x9100)
        }));
    }
}
