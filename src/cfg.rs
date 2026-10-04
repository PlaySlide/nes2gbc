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
  // A valid JSR is strong code evidence too. Do not linear-sweep past it:
  // many NES dispatch routines place raw pointer words immediately after JSR,
  // so continuing here misclassifies legitimate entry points as data.
  if matches!(i.def.mnemonic,Mnemonic::Jmp|Mnemonic::Jsr|Mnemonic::Rts|Mnemonic::Rti|Mnemonic::Brk){return true}
  pc=pc.wrapping_add(i.def.len()as u16);
 }
 true
}

// Detect a non-returning dispatcher that treats the JSR return address as
// the base of an inline word table. Ice Climber uses:
//
//   JSR dispatcher
//   .word state0, state1, ...
//
// dispatcher:
//   ... PLA / STA base / PLA / STA base+1 ...
//   ... LDA (base),Y / STA ptr ...
//   ... LDA (base),Y / STA ptr+1
//   JMP (ptr)
//
// Return (indirect JMP PC, pointer zp) when the entry has this shape.
fn inline_jsr_dispatcher(mapper:u16,prg:&[u8],entry:u16)->Option<(u16,u16)>{
 let start=off(mapper,prg.len(),entry).ok()?;
 let hard_end=(start+48).min(prg.len());

 // Do not let byte-pattern recognition bleed into the next routine. Tennis
 // places an ordinary returning subroutine at $C375 immediately before its
 // real inline-table dispatcher at $C38B; scanning blindly across the RTS at
 // $C38A misclassifies $C375 as non-returning and drops its real return PC.
 let mut end=hard_end;
 let mut scan_pc=entry;
 while let Ok(ins)=dec(mapper,prg,scan_pc){
  let o=match off(mapper,prg.len(),scan_pc){Ok(o)=>o,Err(_)=>break};
  if o>=hard_end{break}
  let next=scan_pc.wrapping_add(ins.def.len()as u16);
  if matches!(ins.def.mnemonic,Mnemonic::Rts|Mnemonic::Rti|Mnemonic::Brk){
   end=(o+ins.def.len() as usize).min(hard_end);
   break
  }
  if matches!(ins.def.mnemonic,Mnemonic::Jmp){
   end=(o+ins.def.len() as usize).min(hard_end);
   break
  }
  scan_pc=next;
 }
 if end<=start+12{return None}

 let mut pop=None;
 let mut i=start;
 while i+5<end{
  if prg[i]==0x68&&prg[i+1]==0x85
   &&prg[i+3]==0x68&&prg[i+4]==0x85
   &&prg[i+5]==prg[i+2].wrapping_add(1)
  {
   pop=Some((i,prg[i+2]));break
  }
  i+=1;
 }
 let (pop_i,base)=pop?;

 // Some Nintendo dispatchers keep the low target byte in X while reusing
 // the popped return-address zero-page pair as the final JMP pointer:
 //
 //   LDA (base),Y
 //   TAX
 //   INY
 //   LDA (base),Y
 //   STA base+1
 //   STX base
 //   JMP (base)
 //
 // Tennis and Dig Dug use this compact variant. Recognize it before the
 // ordinary STA/STA form below, because its second LDA/STA pair otherwise
 // looks like a candidate low-byte store.
 let mut j=pop_i+6;
 while j+2<end{
  if prg[j]==0xB1&&prg[j+1]==base&&prg[j+2]==0xAA{
   let mut k=j+3;
   let k_end=(j+8).min(end.saturating_sub(9));
   while k<=k_end{
    if k+8<end
     &&prg[k]==0xB1&&prg[k+1]==base
     &&prg[k+2]==0x85&&prg[k+3]==base.wrapping_add(1)
     &&prg[k+4]==0x86&&prg[k+5]==base
     &&prg[k+6]==0x6C&&prg[k+7]==base&&prg[k+8]==0x00
    {
     let delta=(k+6-start)as u16;
     return Some((entry.wrapping_add(delta),base as u16))
    }
    k+=1;
   }
  }
  j+=1;
 }

 let mut low=None;
 let mut j=pop_i+6;
 while j+3<end{
  if prg[j]==0xB1&&prg[j+1]==base&&prg[j+2]==0x85{
   low=Some((j,prg[j+3]));break
  }
  j+=1;
 }
 let (low_i,pointer)=low?;

 let mut high=None;
 let mut k=low_i+4;
 while k+3<end{
  if prg[k]==0xB1&&prg[k+1]==base&&prg[k+2]==0x85
   &&prg[k+3]==pointer.wrapping_add(1)
  {
   high=Some(k);break
  }
  k+=1;
 }
 let high_i=high?;

 let mut m=high_i+4;
 while m+2<end{
  if prg[m]==0x6C&&prg[m+1]==pointer&&prg[m+2]==0x00{
   let delta=(m-start)as u16;
   return Some((entry.wrapping_add(delta),pointer as u16))
  }
  m+=1;
 }
 None
}

// A byte-sized dispatcher index can address at most 128 distinct 16-bit
// entries when used as an even byte offset (ASL/TAY is the common NES idiom).
// Use that architectural bound rather than a game-specific guess.
const MAX_WORD_TABLE_ENTRIES:u16=128;

fn inline_word_table_targets(mapper:u16,prg:&[u8],base:u16)->Vec<u16>{
 let mut out=Vec::new();
 for i in 0..MAX_WORD_TABLE_ENTRIES{
  let a=base.wrapping_add(i*2);
  let Ok(o)=off(mapper,prg.len(),a)else{break};
  if o+1>=prg.len(){break}
  let target=u16::from_le_bytes([prg[o],prg[o+1]]);
  if target<0x8000||!looks_like_code(mapper,prg,target){break}
  if !out.contains(&target){out.push(target)}
 }
 out
}

// Recognize the common 6502 jump-table idiom:
//   LDA table,Y / STA ptr / INY / LDA table,Y / STA ptr+1 / ... / JMP (ptr)
// The index is often byte-sized and scaled to an even offset. Walk up to the
// architectural 128-word maximum, stopping at the first non-code destination
// once a table has begun.
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

 // Form 1b: adjacent low/high table bytes without INY:
 //   LDA table,Y   / STA ptr
 //   LDA table+1,Y / STA ptr+1
 //   ... optional selector/state work ...
 //   JMP (ptr)
 //
 // Dig Dug uses this compact form. Y is already an even byte offset, so
 // table/table+1 select the low and high bytes of the same little-endian
 // target. Do not require the JMP to follow immediately: one Dig Dug
 // dispatcher builds the pointer at $D3DC, performs another small state-table
 // lookup, then finally JMPs through $EA at $D3EE.
 let mut pc=start;
 while pc.saturating_add(10)<=jmp_pc{
  let o=match off(mapper,prg.len(),pc){Ok(o)=>o,Err(_)=>break};
  if o+10<=prg.len()
   &&prg[o]==0xB9
   &&prg[o+3]==0x85&&prg[o+4]==pointer as u8
   &&prg[o+5]==0xB9
   &&prg[o+8]==0x85&&prg[o+9]==pointer.wrapping_add(1)as u8
  {
   let base=u16::from_le_bytes([prg[o+1],prg[o+2]]);
   let high_base=u16::from_le_bytes([prg[o+6],prg[o+7]]);
   if high_base==base.wrapping_add(1)&&!tables.contains(&base){tables.push(base)}
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

 // Form 3: a non-returning JSR dispatcher indexes a word table placed
 // immediately after each JSR, using the stacked return address as its base.
 // Ice Climber uses this extensively.
 let mut call_off=0usize;
 while call_off+3<=prg.len(){
  if prg[call_off]==0x20{
   let entry=u16::from_le_bytes([prg[call_off+1],prg[call_off+2]]);
   if let Some((dispatch_jmp,dispatch_ptr))=inline_jsr_dispatcher(mapper,prg,entry){
    if dispatch_jmp==jmp_pc&&dispatch_ptr==pointer{
     let base=0x8000u16.wrapping_add((call_off+3)as u16);
     if !tables.contains(&base){tables.push(base)}
    }
   }
  }
  call_off+=1;
 }

 // Form 4: pointer construction anywhere in PRG that reaches this JMP (ptr)
 // through straight-line code, possibly via JMP abs into a shared tail:
 //   LDA table,R / STA ptr / [INR] / LDA table(+1),R / JMP tail
 //   tail: STA ptr+1 / JMP (ptr)
 // A tiny forward walk tracks whether A still holds the high-byte load when
 // ptr+1 is stored and requires reaching exactly this JMP (ptr).
 if let Ok(jmp_off)=off(mapper,prg.len(),jmp_pc){
  let mut o=0usize;
  while o+5<=prg.len(){
   if (prg[o]==0xB9||prg[o]==0xBD)&&prg[o+3]==0x85&&prg[o+4]==pointer as u8{
    let base=u16::from_le_bytes([prg[o+1],prg[o+2]]);
    if let Some(b)=walk_pointer_tail(mapper,prg,o,jmp_off,pointer,base){
     if !tables.contains(&b){tables.push(b)}
    }
   }
   o+=1;
  }
 }

 // Form 5: a shared dispatcher that dereferences a table pointer itself:
 //   disp: ... LDA (B),Y / ... / LDA (B),Y / ... / JMP (ptr)
 // with callers that point B at a word table and JSR/JMP into disp:
 //   LDA #<table / STA B / LDA #>table / STA B+1 / ... / JSR disp
 // or select the table from a table of tables:
 //   LDA tables,X / STA B / LDA tables+1,X / STA B+1 / ... / JMP disp
 // Ice Hockey dispatches its game modes this way ($80D6/$837D -> $8359).
 if let Ok(jmp_off)=off(mapper,prg.len(),jmp_pc){
  let lo=jmp_off.saturating_sub(40);
  let mut bases=Vec::new();
  for o in lo..jmp_off{if prg[o]==0xB1&&!bases.contains(&prg[o+1]){bases.push(prg[o+1])}}
  for b in bases{
   if b==0xFF{continue}
   let mut o=0usize;
   while o+8<=prg.len(){
    let imm=prg[o]==0xA9&&prg[o+2]==0x85&&prg[o+3]==b&&prg[o+4]==0xA9&&prg[o+6]==0x85&&prg[o+7]==b+1;
    let imm_rev=prg[o]==0xA9&&prg[o+2]==0x85&&prg[o+3]==b+1&&prg[o+4]==0xA9&&prg[o+6]==0x85&&prg[o+7]==b;
    let idx=o+10<=prg.len()&&(prg[o]==0xBD||prg[o]==0xB9)&&prg[o+3]==0x85&&prg[o+4]==b&&prg[o+5]==prg[o]&&prg[o+8]==0x85&&prg[o+9]==b+1
     &&u16::from_le_bytes([prg[o+6],prg[o+7]])==u16::from_le_bytes([prg[o+1],prg[o+2]]).wrapping_add(1);
    if imm||imm_rev||idx{
     let site_end=o+if idx{10}else{8};
     if reaches_dispatcher(mapper,prg,site_end,b,lo,jmp_off){
      if imm||imm_rev{
       let t=if imm{u16::from_le_bytes([prg[o+1],prg[o+5]])}else{u16::from_le_bytes([prg[o+5],prg[o+1]])};
       if !tables.contains(&t){tables.push(t)}
      }else{
       let tt=u16::from_le_bytes([prg[o+1],prg[o+2]]);
       for i in 0..MAX_WORD_TABLE_ENTRIES{
        let Ok(x)=off(mapper,prg.len(),tt.wrapping_add(i*2))else{break};
        if x+1>=prg.len(){break}
        let sub=u16::from_le_bytes([prg[x],prg[x+1]]);
        if sub<0x8000{break}
        if !tables.contains(&sub){tables.push(sub)}
       }
      }
     }
    }
    o+=1;
   }
  }
 }

 let mut out=Vec::new();
 for base in tables{
  let mut found_any=false;
  for i in 0..MAX_WORD_TABLE_ENTRIES{
   let a=base.wrapping_add(i*2);
   let Ok(o)=off(mapper,prg.len(),a)else{break};
   if o+1>=prg.len(){break}
   let target=u16::from_le_bytes([prg[o],prg[o+1]]);
   let valid=target>=0x8000&&looks_like_code(mapper,prg,target);
   if !valid{
    if found_any{break}
    continue
   }
   found_any=true;
   if !out.contains(&target){out.push(target)}
  }
 }
 out
}
// Helper for indirect_table_targets Form 5: from PRG offset `o` (just after
// the B/B+1 pointer setup), straight-line code must JSR/JMP into the
// dispatcher window [lo, jmp_off] without touching B/B+1.
fn reaches_dispatcher(mapper:u16,prg:&[u8],o:usize,b:u8,lo:usize,jmp_off:usize)->bool{
 let mut pc=0x8000u16.wrapping_add(o as u16);
 for _ in 0..6{
  let Ok(i)=dec(mapper,prg,pc)else{return false};
  use Mnemonic::*;
  match i.def.mnemonic{
   Jsr|Jmp if i.def.mode==AddressingMode::Absolute=>{
    let Ok(t)=off(mapper,prg.len(),i.operand)else{return false};
    return t>=lo&&t<=jmp_off;
   }
   Sta|Stx|Sty|Inc|Dec|Asl|Lsr|Rol|Ror if i.def.mode==AddressingMode::ZeroPage&&(i.operand==b as u16||i.operand==b as u16+1)=>return false,
   Lda|Ldx|Ldy|Tax|Tay|Txa|Tya|Asl|Lsr|Clc|Sec|Adc|Sbc|And|Ora|Eor|Sta|Stx|Sty|Cmp|Nop=>{}
   _=>return false,
  }
  pc=pc.wrapping_add(i.def.len()as u16);
 }
 false
}
// Helper for indirect_table_targets Form 4. `o` is the PRG offset of
// `LDA base,R / STA ptr`. Returns the table base when straight-line code from
// there loads the matching high byte (base+1 with the same index, or base after
// one INY/INX), stores it to ptr+1 and reaches the JMP (ptr) at `jmp_off`.
fn walk_pointer_tail(mapper:u16,prg:&[u8],o:usize,jmp_off:usize,pointer:u16,base:u16)->Option<u16>{
 let index_y=prg[o]==0xB9;
 let mut pc=0x8000u16.wrapping_add((o+5)as u16);
 let mut inc=0u8;
 let mut a_hi:Option<u16>=None; // high-byte table loaded into A
 let mut hi_stored=false;
 for _ in 0..16{
  let i=dec(mapper,prg,pc).ok()?;
  let cur=off(mapper,prg.len(),pc).ok()?;
  let next=pc.wrapping_add(i.def.len()as u16);
  use Mnemonic::*;
  match i.def.mnemonic{
   Jmp if i.def.mode==AddressingMode::Indirect=>{
    if cur!=jmp_off||i.operand!=pointer||!hi_stored{return None}
    return Some(base);
   }
   Jmp=>{pc=i.operand;continue}
   Iny if index_y=>{inc+=1}
   Inx if !index_y=>{inc+=1}
   Lda if (index_y&&i.def.mode==AddressingMode::AbsoluteY)||(!index_y&&i.def.mode==AddressingMode::AbsoluteX)=>{
    let t=i.operand;
    a_hi=if (inc==0&&t==base.wrapping_add(1))||(inc==1&&t==base){Some(t)}else{None};
   }
   Sta|Stx|Sty if i.def.mode==AddressingMode::ZeroPage&&i.operand==pointer=>return None,
   Sta if i.def.mode==AddressingMode::ZeroPage&&i.operand==pointer.wrapping_add(1)=>{
    if a_hi.is_none(){return None}
    hi_stored=true;
   }
   Stx|Sty if i.def.mode==AddressingMode::ZeroPage&&i.operand==pointer.wrapping_add(1)=>return None,
   Sta|Stx|Sty|Nop|Clc|Sec|Cld|Cli|Sei|Clv|Php|Pha=>{}
   Ldx if index_y=>{}
   Ldy if !index_y=>{}
   Tax if index_y=>{}
   Tay if !index_y=>{}
   Cmp|Cpx|Cpy|Bit=>{}
   Asl|Lsr|Rol|Ror if i.def.mode==AddressingMode::Accumulator=>{a_hi=None}
   Asl|Lsr|Rol|Ror|Inc|Dec=>{if i.operand==pointer||i.operand==pointer.wrapping_add(1){return None}}
   Lda|Txa|Tya|Pla|Adc|Sbc|And|Ora|Eor=>{a_hi=None}
   _=>return None,
  }
  pc=next;
 }
 None
}
// Recognize the classic 6502 computed-jump-via-RTS idiom:
//
//   LDA table+1,Y   ; or ,X
//   PHA
//   LDA table,Y
//   PHA
//   RTS
//
// RTS pulls the synthetic return address and increments it, so each table word
// stores target-1. Bomberman's music/effect dispatchers use both indexed forms.
fn rts_stack_table_targets(mapper:u16,prg:&[u8],rts_pc:u16)->Vec<u16>{
 let Ok(rts_off)=off(mapper,prg.len(),rts_pc)else{return Vec::new()};
 if rts_off<8{return Vec::new()}
 let o=rts_off-8;
 let index_op=prg[o];
 if index_op!=0xB9&&index_op!=0xBD{return Vec::new()} // LDA abs,Y / LDA abs,X
 if prg[o+3]!=0x48||prg[o+4]!=index_op||prg[o+7]!=0x48||prg[o+8]!=0x60{return Vec::new()}
 let high_base=u16::from_le_bytes([prg[o+1],prg[o+2]]);
 let low_base=u16::from_le_bytes([prg[o+5],prg[o+6]]);
 if high_base!=low_base.wrapping_add(1){return Vec::new()}

 let mut out=Vec::new();
 let mut found_any=false;
 for i in 0..MAX_WORD_TABLE_ENTRIES{
  let a=low_base.wrapping_add(i*2);
  let Ok(t)=off(mapper,prg.len(),a)else{break};
  if t+1>=prg.len(){break}
  let raw=u16::from_le_bytes([prg[t],prg[t+1]]);
  let target=raw.wrapping_add(1);
  let valid=target>=0x8000&&looks_like_code(mapper,prg,target);
  if !valid{
   // Some dispatchers deliberately index from a couple of bytes before the
   // first real word. Bomberman does this at $D244: Y starts at 2, so the
   // nominal entry 0 is junk ($6005) and the first real stored target-1 is
   // $D320 at +2 -> destination $D321.
   if found_any{break}
   continue
  }
  found_any=true;
  if !out.contains(&target){out.push(target)}
 }
 out
}

// Recognize a constant return address pushed before a computed jump:
//
//   LDA #>(continuation-1)
//   PHA
//   LDA #<(continuation-1)
//   PHA
//   ... (usually a stack-built RTS jump table or JMP (ptr))
//
// The callee's eventual RTS resumes at continuation, which is never the
// fallthrough of a JSR, so it must be discovered as its own dispatch entry.
// Bomberman does this at $CFD4 (continuation $CFE3).
fn pushed_return_continuation(mapper:u16,prg:&[u8],ins:&[DecodedInstruction])->Option<u16>{
 let n=ins.len();
 if n<4{return None}
 let w=&ins[n-4..];
 let imm_lda=|i:&DecodedInstruction|i.def.mnemonic==Mnemonic::Lda&&i.def.mode==AddressingMode::Immediate;
 if !(imm_lda(&w[0])&&w[1].def.mnemonic==Mnemonic::Pha&&imm_lda(&w[2])&&w[3].def.mnemonic==Mnemonic::Pha){return None}
 let raw=((w[0].operand as u16&0xFF)<<8)|(w[2].operand&0xFF);
 let target=raw.wrapping_add(1);
 if target<0x8000||!looks_like_code(mapper,prg,target){return None}
 Some(target)
}

// Truncate an existing block that contains `t` as an interior instruction
// boundary, ending it with a fallthrough to `t`. Returns true if split.
fn split_block_at(blocks:&mut BTreeMap<u16,BasicBlock>,t:u16)->bool{
 let Some((&start,_))=blocks.range(..t).next_back()else{return false};
 let b=blocks.get_mut(&start).unwrap();
 let Some(idx)=b.instructions.iter().position(|i|i.pc==t)else{return false};
 if idx==0{return false}
 b.instructions.truncate(idx);
 b.edges=vec![Edge{kind:EdgeKind::Fallthrough,target:Some(t)}];
 true
}

fn reachable_blocks(blocks:&BTreeMap<u16,BasicBlock>,roots:impl Iterator<Item=u16>)->BTreeSet<u16>{
 let mut seen=BTreeSet::new();let mut work:Vec<u16>=roots.collect();
 while let Some(a)=work.pop(){
  if !seen.insert(a){continue}
  if let Some(b)=blocks.get(&a){for t in b.edges.iter().filter_map(|e|e.target){if blocks.contains_key(&t)&&!seen.contains(&t){work.push(t)}}}
 }
 seen.retain(|a|blocks.contains_key(a));
 seen
}

fn anchored_word_table_targets(mapper:u16,prg:&[u8],blocks:&BTreeMap<u16,BasicBlock>,confirmed:&BTreeSet<u16>,gaps:&BTreeSet<u16>)->Vec<u16>{
 const MIN_RUN:usize=3;
 let n=prg.len();
 // 16 KiB PRG is mirrored; only accept the half the vectors execute from so
 // unrelated data words in the other mirror do not extend a run.
 let half=if n==0x4000{Some(u16::from_le_bytes([prg[n-4],prg[n-3]])&0xC000)}else{None};
 let ok=|w:u16|->bool{
  if w<0x8000{return false}
  if let Some(h)=half{if w&0xC000!=h{return false}}
  looks_like_code(mapper,prg,w)
 };
 // Bytes already decoded as instructions are code, not table data. This keeps
 // compare/branch chains (C9 07 F0 03 ...) from reading as pointer runs.
 let mut code=vec![false;n];
 for b in blocks.values(){for i in &b.instructions{
  if let Ok(o)=off(mapper,n,i.pc){for k in 0..i.def.len()as usize{if o+k<n{code[o+k]=true}}}
 }}
 let mut out=Vec::new();
 for align in 0..2usize{
  let mut run:Vec<u16>=Vec::new();
  let mut o=align;
  loop{
   let w=if o+1<n{Some(u16::from_le_bytes([prg[o],prg[o+1]]))}else{None};
   match w{
    Some(w) if !code[o]&&!code[o+1]&&ok(w)=>run.push(w),
    _=>{
     let distinct:BTreeSet<u16>=run.iter().copied().collect();
     let known=distinct.iter().filter(|t|confirmed.contains(t)).count();
     let gap_hits=distinct.iter().filter(|t|!confirmed.contains(t)&&gaps.contains(t)).count();
     let anchors=known+gap_hits;
     // Short data runs often hit two block starts by coincidence; a gap hit
     // (routine start after a terminator) is much stronger evidence.
     let accept=gap_hits>=2||(anchors>=3&&run.len()>=6);
     if std::env::var_os("NES2GBC_CFG_DEBUG").is_some()&&distinct.len()>=MIN_RUN&&anchors>=2{
      eprint!("{} ",if accept{"ACCEPT"}else{"reject"});
      eprintln!("cfg: run @{:04X} len {} distinct {} confirmed {}: {}",0x10000-n+o-2*run.len(),run.len(),distinct.len(),anchors,run.iter().map(|t|format!("{}{:04X}",if confirmed.contains(t){"*"}else if gaps.contains(t){"+"}else{""},t)).collect::<Vec<_>>().join(" "));
     }
     if distinct.len()>=MIN_RUN&&accept{
      for &t in &run{if !out.contains(&t){out.push(t)}}
     }
     run.clear();
     if w.is_none(){break}
    }
   }
   o+=2;
  }
 }
 out
}

pub fn discover_from_vectors(mapper:u16,prg:&[u8],v:Vectors)->Result<ControlFlowGraph,AnalysisError>{discover(mapper,prg,&[v.reset,v.nmi,v.irq_brk])}
pub fn discover(mapper:u16,prg:&[u8],entries:&[u16])->Result<ControlFlowGraph,AnalysisError>{
 if mapper!=0&&mapper!=3{return Err(AnalysisError::UnsupportedMapper(mapper))}if prg.len()!=0x4000&&prg.len()!=0x8000{return Err(AnalysisError::UnsupportedPrgSize(prg.len()))}
 let mut work=VecDeque::new();let mut seen=BTreeSet::new();for &e in entries{q(&mut work,&mut seen,e)}
 let mut blocks=BTreeMap::new();let mut diagnostics=Vec::new();let mut continuations:Vec<u16>=Vec::new();let mut harvested:Vec<u16>=Vec::new();
 let cfg_debug=std::env::var_os("NES2GBC_CFG_DEBUG").is_some();
 loop{
 while let Some(start)=work.pop_front(){if blocks.contains_key(&start){continue}let mut pc=start;let mut ins=Vec::new();let mut edges=Vec::new();
  loop{
   if pc!=start&&(blocks.contains_key(&pc)||seen.contains(&pc)){edges.push(Edge{kind:EdgeKind::Fallthrough,target:Some(pc)});break}
   let i=match dec(mapper,prg,pc){Ok(i)=>i,Err(AnalysisError::Decode(error))=>{diagnostics.push(AnalysisDiagnostic{pc,error});break},Err(AnalysisError::UnmappedAddress(_))if pc!=start=>break,Err(e)=>return Err(e)};
   let next=pc.wrapping_add(i.def.len()as u16);ins.push(i);
   if branch(i.def.mnemonic){let t=rel(i);edges.push(Edge{kind:EdgeKind::BranchTaken,target:Some(t)});edges.push(Edge{kind:EdgeKind::Fallthrough,target:Some(next)});q(&mut work,&mut seen,t);q(&mut work,&mut seen,next);break}
   match i.def.mnemonic{
    Mnemonic::Jsr=>{
     let t=i.operand;
     edges.push(Edge{kind:EdgeKind::Call,target:Some(t)});
     q(&mut work,&mut seen,t);
     if let Some((_,pointer))=inline_jsr_dispatcher(mapper,prg,t){
      let targets=inline_word_table_targets(mapper,prg,next);
      if !targets.is_empty(){
       for target in targets{
        edges.push(Edge{kind:EdgeKind::IndirectJump{pointer},target:Some(target)});
        q(&mut work,&mut seen,target);
       }
       break
      }
     }
     edges.push(Edge{kind:EdgeKind::CallReturn,target:Some(next)});
     q(&mut work,&mut seen,next);
     break
    }
    Mnemonic::Jmp if i.def.mode==AddressingMode::Absolute=>{let t=i.operand;edges.push(Edge{kind:EdgeKind::Jump,target:Some(t)});q(&mut work,&mut seen,t);break}
    Mnemonic::Jmp if i.def.mode==AddressingMode::Indirect=>{
     let targets=indirect_table_targets(mapper,prg,i.pc,i.operand);
     if targets.is_empty(){edges.push(Edge{kind:EdgeKind::IndirectJump{pointer:i.operand},target:None});}
     else{for t in targets{edges.push(Edge{kind:EdgeKind::IndirectJump{pointer:i.operand},target:Some(t)});q(&mut work,&mut seen,t);}}
     break
    }
    Mnemonic::Rts=>{
     let targets=rts_stack_table_targets(mapper,prg,i.pc);
     for t in targets{
      // The stack, rather than a zero-page pointer, supplies the computed PC.
      // Reuse IndirectJump for reachability; code generation still lowers the
      // actual RTS instruction normally.
      edges.push(Edge{kind:EdgeKind::IndirectJump{pointer:0x0100},target:Some(t)});
      q(&mut work,&mut seen,t);
     }
     break
    }
    Mnemonic::Rti|Mnemonic::Brk=>break,
    Mnemonic::Pha=>{
     // No edge reaches the continuation (a later RTS does, dynamically), so
     // register it as an entry point: code selection and dispatch tables are
     // seeded from entry points.
     if let Some(t)=pushed_return_continuation(mapper,prg,&ins){q(&mut work,&mut seen,t);if !continuations.contains(&t){continuations.push(t)}}
     pc=next
    }
    _=>pc=next
   }
  } blocks.insert(start,BasicBlock{start,instructions:ins,edges});
 }
  // Static discovery converged. Look for jump tables whose indexing code we
  // could not decode (gapped tables, two-level tables, pointer-to-table
  // setups): runs of >=3 consecutive little-endian words in non-code bytes
  // that all point at plausible code, with >=3 distinct values. Evidence for
  // a member is being an already-discovered block, or (stronger) sitting
  // directly after an unconditional terminator (JMP/RTS/RTI), where an
  // otherwise-unreached routine usually starts. A run is accepted with >=2
  // such gap hits, or >=3 pieces of evidence in a run of >=6 words.
  // Excitebike ($C03C table: $C2BD, $C514, ...) and Kung Fu ($8000 table
  // tail: $84F0, $8427, ...) need this.
  let mut added=false;
  // Only blocks reachable without earlier word-table guesses may confirm a
  // run; otherwise a single wrong guess decodes data whose bytes confirm more
  // wrong runs (seen with Ice Hockey).
  let confirmed=reachable_blocks(&blocks,entries.iter().chain(continuations.iter()).copied());
  let gaps:BTreeSet<u16>=blocks.values().filter(|b|confirmed.contains(&b.start)).filter_map(|b|{
   let last=b.instructions.last()?;
   if !matches!(last.def.mnemonic,Mnemonic::Jmp|Mnemonic::Rts|Mnemonic::Rti){return None}
   Some(last.pc.wrapping_add(last.def.len()as u16))
  }).collect();
  for t in anchored_word_table_targets(mapper,prg,&blocks,&confirmed,&gaps){
   if blocks.contains_key(&t){continue}
   if cfg_debug{eprintln!("cfg: word-table entry ${t:04X}")}
   // A target already `seen` may be an interior instruction of a block built
   // earlier; split so the code is translated once under its own label.
   if seen.contains(&t){if split_block_at(&mut blocks,t){work.push_back(t);added=true;if !harvested.contains(&t){harvested.push(t)}}continue}
   q(&mut work,&mut seen,t);
   if !harvested.contains(&t){harvested.push(t)}
   added=true;
  }
  if !added{break}
 }
 let mut ep=entries.to_vec();ep.extend(continuations);ep.extend(harvested);ep.sort_unstable();ep.dedup();Ok(ControlFlowGraph{blocks,entry_points:ep,diagnostics})
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
    fn discovers_shared_tail_pointer_table() {
        // Excitebike: LDA $C000,Y / STA $00 / INY / LDA $C000,Y / JMP tail
        //             tail: STA $01 / JMP ($0000)
        let mut prg = vec![0x00; 0x8000];
        put(&mut prg, 0x9000, &[0xB9, 0x00, 0xA0, 0x85, 0x00, 0xC8, 0xB9, 0x00, 0xA0, 0x4C, 0x20, 0x90]);
        put(&mut prg, 0x9020, &[0x85, 0x01, 0x6C, 0x00, 0x00]);
        put(&mut prg, 0xA000, &[0x00, 0x92, 0x10, 0x92]);
        put(&mut prg, 0x9200, &[0x60]);
        put(&mut prg, 0x9210, &[0x60]);
        let graph = discover(0, &prg, &[0x9000]).unwrap();
        assert!(graph.blocks.contains_key(&0x9200));
        assert!(graph.blocks.contains_key(&0x9210));
    }

    #[test]
    fn harvests_word_table_with_routines_after_terminators() {
        // Unrecognized indexing; the table's members start right after RTS.
        let mut prg = vec![0x00; 0x8000];
        put(&mut prg, 0x9000, &[0x20, 0x00, 0x91, 0x20, 0x10, 0x91, 0x60]);
        put(&mut prg, 0x9100, &[0xA9, 0x01, 0x60, 0xA9, 0x02, 0x60]); // $9103 after RTS
        put(&mut prg, 0x9110, &[0xA9, 0x03, 0x60, 0xA9, 0x04, 0x60]); // $9113 after RTS
        put(&mut prg, 0xA000, &[0x03, 0x91, 0x13, 0x91, 0x00, 0x91]);
        let graph = discover(0, &prg, &[0x9000]).unwrap();
        assert!(graph.blocks.contains_key(&0x9103));
        assert!(graph.blocks.contains_key(&0x9113));
        assert!(graph.entry_points.contains(&0x9103));
    }

    #[test]
    fn discovers_tables_passed_to_dereferencing_dispatcher() {
        // Ice Hockey: callers point $0A at a word table, then JSR a shared
        // dispatcher that loads the target through ($0A),Y.
        let mut prg = vec![0x00; 0x8000];
        put(&mut prg, 0x9000, &[0xA9, 0x00, 0x85, 0x0A, 0xA9, 0xA0, 0x85, 0x0B, 0xA5, 0x08, 0x20, 0x00, 0x91, 0x60]);
        put(
            &mut prg,
            0x9100,
            &[
                0x0A, 0xA8,       // ASL / TAY
                0xB1, 0x0A, 0x48, // LDA ($0A),Y / PHA
                0xC8, 0xB1, 0x0A, // INY / LDA ($0A),Y
                0x85, 0x0B, 0x68, 0x85, 0x0A, // STA $0B / PLA / STA $0A
                0x6C, 0x0A, 0x00, // JMP ($000A)
            ],
        );
        put(&mut prg, 0xA000, &[0x00, 0x92, 0x10, 0x92]);
        put(&mut prg, 0x9200, &[0x60]);
        put(&mut prg, 0x9210, &[0x60]);
        let graph = discover(0, &prg, &[0x9000]).unwrap();
        assert!(graph.blocks.contains_key(&0x9200));
        assert!(graph.blocks.contains_key(&0x9210));
    }

    #[test]
    fn discovers_pushed_constant_return_continuation() {
        let mut prg = vec![0xEA; 0x8000];
        put(
            &mut prg,
            0x9000,
            &[
                0xA9, 0x90, 0x48, // LDA #$90 / PHA
                0xA9, 0x0F, 0x48, // LDA #$0F / PHA -> continuation $9010
                0x6C, 0x00, 0x02, // JMP ($0200), unresolved
            ],
        );
        put(&mut prg, 0x9010, &[0x60]);
        let graph = discover(0, &prg, &[0x9000]).unwrap();
        assert!(graph.blocks.contains_key(&0x9010));
        assert!(graph.entry_points.contains(&0x9010));
    }

    #[test]
    fn discovers_indexed_push_rts_jump_table_targets() {
        let mut prg = vec![0xEA; 0x8000];

        put(
            &mut prg,
            0x9000,
            &[
                0xB9, 0x01, 0xA0, // LDA $A001,Y (high byte)
                0x48,             // PHA
                0xB9, 0x00, 0xA0, // LDA $A000,Y (low byte)
                0x48,             // PHA
                0x60,             // RTS -> synthetic target + 1
            ],
        );
        // The nominal base can begin with a non-target word; Bomberman has one
        // such leading slot because the runtime index starts at 2. Real stored
        // words are target-1.
        put(&mut prg, 0xA000, &[0x05, 0x60, 0xFF, 0x91, 0x0F, 0x92, 0x00, 0x00]);
        put(&mut prg, 0x9200, &[0x60]);
        put(&mut prg, 0x9210, &[0x60]);

        let graph = discover(0, &prg, &[0x9000]).unwrap();

        assert!(graph.blocks.contains_key(&0x9200));
        assert!(graph.blocks.contains_key(&0x9210));
        let dispatcher = graph.blocks.get(&0x9000).unwrap();
        assert!(dispatcher.edges.iter().any(|edge| {
            matches!(edge.kind, EdgeKind::IndirectJump { pointer: 0x0100 })
                && edge.target == Some(0x9200)
        }));

        // Bomberman's other sound tables use the same trick indexed by X.
        put(
            &mut prg,
            0x9010,
            &[
                0xBD, 0x01, 0xA0, // LDA $A001,X
                0x48,
                0xBD, 0x00, 0xA0, // LDA $A000,X
                0x48,
                0x60,
            ],
        );
        assert_eq!(rts_stack_table_targets(0, &prg, 0x9018), vec![0x9200, 0x9210]);
    }

    #[test]
    fn discovers_adjacent_low_high_indexed_jump_table_targets() {
        let mut prg = vec![0xEA; 0x8000];

        put(
            &mut prg,
            0x9000,
            &[
                0xB9, 0x00, 0xA0, // LDA $A000,Y
                0x85, 0xEA,       // STA $EA
                0xB9, 0x01, 0xA0, // LDA $A001,Y
                0x85, 0xEB,       // STA $EB
                0xAD, 0x60, 0x04, // unrelated state read
                0x4A,             // LSR
                0x29, 0x01,       // AND #1
                0xA8,             // TAY
                0x6C, 0xEA, 0x00, // JMP ($00EA)
            ],
        );

        // Y is an even byte offset into a little-endian word table.
        put(&mut prg, 0xA000, &[0x00, 0x92, 0x10, 0x92, 0x20, 0x92, 0x00, 0x00]);
        put(&mut prg, 0x9200, &[0x60]);
        put(&mut prg, 0x9210, &[0x60]);
        put(&mut prg, 0x9220, &[0x60]);

        let graph = discover(0, &prg, &[0x9000]).unwrap();

        assert!(graph.blocks.contains_key(&0x9200));
        assert!(graph.blocks.contains_key(&0x9210));
        assert!(graph.blocks.contains_key(&0x9220));
        let dispatcher = graph.blocks.get(&0x9000).unwrap();
        assert!(dispatcher.edges.iter().any(|edge| {
            matches!(edge.kind, EdgeKind::IndirectJump { pointer: 0x00EA })
                && edge.target == Some(0x9200)
        }));
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

        let mut table = [0u8; 32];
        table[0..4].copy_from_slice(&[0x00, 0x92, 0x10, 0x92]);
        put(&mut prg, 0xA000, &table);
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
    fn code_probe_accepts_jsr_before_inline_pointer_data() {
        let mut prg = vec![0xEA; 0x8000];

        // Target begins with real code and then an inline pointer table.
        // A linear probe that walks past JSR would hit $DC and reject it.
        put(
            &mut prg,
            0x8231,
            &[
                0xAD, 0x72, 0x07,       // LDA $0772
                0x20, 0x04, 0x8E,       // JSR $8E04
                0xCF, 0x8F, 0x67, 0x85, // inline .word data
                0x61, 0x90, 0x45, 0x82,
            ],
        );
        put(&mut prg, 0x8E04, &[0x60]);

        assert!(looks_like_code(0, &prg, 0x8231));
    }

    #[test]
    fn x_temp_dispatcher_probe_does_not_cross_prior_rts() {
        let mut prg = vec![0xEA; 0x8000];

        // Ordinary returning subroutine immediately followed by a genuine
        // TAX/STX inline-table dispatcher, matching Tennis $C375/$C38B.
        put(
            &mut prg,
            0x9000,
            &[
                0xA2, 0x03,       // LDX #3
                0xC6, 0x20,       // DEC $20
                0x10, 0x02,       // BPL +2
                0xEA, 0xEA,       // harmless body
                0x60,             // RTS
                0x0A,             // dispatcher begins here
                0xA8,
                0xC8,
                0x68, 0x85, 0x14,
                0x68, 0x85, 0x15,
                0xB1, 0x14,
                0xAA,
                0xC8,
                0xB1, 0x14,
                0x85, 0x15,
                0x86, 0x14,
                0x6C, 0x14, 0x00,
            ],
        );

        assert!(inline_jsr_dispatcher(0, &prg, 0x9000).is_none());
        assert!(inline_jsr_dispatcher(0, &prg, 0x9009).is_some());
    }

    #[test]
    fn discovers_inline_table_dispatcher_with_x_temp() {
        let mut prg = vec![0xEA; 0x8000];

        put(
            &mut prg,
            0x9000,
            &[
                0x20, 0x00, 0x91, // JSR $9100
                0x00, 0x92,       // .word $9200
                0x10, 0x92,       // .word $9210
                0x00, 0x00,       // terminator / following non-code
            ],
        );
        put(
            &mut prg,
            0x9100,
            &[
                0x0A,             // ASL
                0xA8,             // TAY
                0xC8,             // INY
                0x68, 0x85, 0x14, // PLA / STA $14
                0x68, 0x85, 0x15, // PLA / STA $15
                0xB1, 0x14,       // LDA ($14),Y
                0xAA,             // TAX (hold low target byte)
                0xC8,             // INY
                0xB1, 0x14,       // LDA ($14),Y
                0x85, 0x15,       // STA $15 (high target byte)
                0x86, 0x14,       // STX $14 (low target byte)
                0x6C, 0x14, 0x00, // JMP ($0014)
            ],
        );
        put(&mut prg, 0x9200, &[0x60]);
        put(&mut prg, 0x9210, &[0x60]);

        let graph = discover(0, &prg, &[0x9000]).unwrap();

        assert!(graph.blocks.contains_key(&0x9200));
        assert!(graph.blocks.contains_key(&0x9210));
        assert!(!graph.blocks.contains_key(&0x9003));
        let dispatcher = graph.blocks.get(&0x9100).unwrap();
        assert!(dispatcher.edges.iter().any(|edge| {
            matches!(edge.kind, EdgeKind::IndirectJump { pointer: 0x0014 })
                && edge.target == Some(0x9200)
        }));
    }

    #[test]
    fn discovers_inline_table_after_nonreturning_jsr_dispatcher() {
        let mut prg = vec![0xEA; 0x8000];

        put(
            &mut prg,
            0x9000,
            &[
                0x20, 0x00, 0x91, // JSR $9100
                0x00, 0x92,       // .word $9200
                0x10, 0x92,       // .word $9210
                0x00, 0x00,       // terminator / following non-code
            ],
        );
        put(
            &mut prg,
            0x9100,
            &[
                0xA5, 0x55,       // LDA $55
                0x0A,             // ASL
                0xA8,             // TAY
                0x68, 0x85, 0x00, // PLA / STA $00
                0x68, 0x85, 0x01, // PLA / STA $01
                0xC8,
                0xB1, 0x00,       // LDA ($00),Y
                0x85, 0x02,       // STA $02
                0xC8,
                0xB1, 0x00,       // LDA ($00),Y
                0x85, 0x03,       // STA $03
                0x6C, 0x02, 0x00, // JMP ($0002)
            ],
        );
        put(&mut prg, 0x9200, &[0x60]);
        put(&mut prg, 0x9210, &[0x60]);

        let graph = discover(0, &prg, &[0x9000]).unwrap();

        assert!(graph.blocks.contains_key(&0x9200));
        assert!(graph.blocks.contains_key(&0x9210));
        assert!(!graph.blocks.contains_key(&0x9003));
        let dispatcher = graph.blocks.get(&0x9100).unwrap();
        assert!(dispatcher.edges.iter().any(|edge| {
            matches!(edge.kind, EdgeKind::IndirectJump { pointer: 0x0002 })
                && edge.target == Some(0x9200)
        }));
        assert!(dispatcher.edges.iter().any(|edge| {
            matches!(edge.kind, EdgeKind::IndirectJump { pointer: 0x0002 })
                && edge.target == Some(0x9210)
        }));
    }

    #[test]
    fn discovers_long_inline_dispatch_table_targets() {
        let mut prg = vec![0xEA; 0x8000];

        // SMB's area-object JumpEngine table has more than 32 entries; the
        // flagpole handler is entry 35. Make sure non-returning inline-table
        // discovery reaches targets beyond the old 32-word cutoff.
        put(
            &mut prg,
            0x9000,
            &[
                0x20, 0x00, 0x91, // JSR $9100
            ],
        );
        put(
            &mut prg,
            0x9100,
            &[
                0xA5, 0x55,       // LDA $55
                0x0A,             // ASL
                0xA8,             // TAY
                0x68, 0x85, 0x00, // PLA / STA $00
                0x68, 0x85, 0x01, // PLA / STA $01
                0xC8,
                0xB1, 0x00,
                0x85, 0x02,
                0xC8,
                0xB1, 0x00,
                0x85, 0x03,
                0x6C, 0x02, 0x00, // JMP ($0002)
            ],
        );

        // 80 valid entries. Entry 35 mirrors SMB's flagpole position, while
        // entry 79 proves discovery is no longer truncated at the old 64-word
        // emergency limit.
        for i in 0..80u16 {
            let target = 0xA000u16 + i * 0x10;
            let a = 0x9003u16 + i * 2;
            put(&mut prg, a, &target.to_le_bytes());
            put(&mut prg, target, &[0x60]); // RTS
        }
        // Stop table discovery cleanly after the valid entries.
        put(&mut prg, 0x9003 + 80 * 2, &[0x00, 0x00]);

        let graph = discover(0, &prg, &[0x9000]).unwrap();
        assert!(graph.blocks.contains_key(&(0xA000 + 35 * 0x10)));
        assert!(graph.blocks.contains_key(&(0xA000 + 79 * 0x10)));
    }

    #[test]
    fn discovers_indexed_indirect_targets_beyond_sixteen_entries() {
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

        // Twenty-one valid word entries: the old 16-entry scan could never
        // discover the distinct handler at entry 20.
        for i in 0..21u16 {
            let target = if i == 20 { 0x9300u16 } else { 0x9200u16 };
            put(&mut prg, 0xA000 + i * 2, &target.to_le_bytes());
        }
        put(&mut prg, 0xA000 + 21 * 2, &[0x00, 0x00]);
        put(&mut prg, 0x9200, &[0x60]);
        put(&mut prg, 0x9300, &[0x60]);

        let graph = discover(0, &prg, &[0x9000]).unwrap();
        assert!(graph.blocks.contains_key(&0x9300));
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
