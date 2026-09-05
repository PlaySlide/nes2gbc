use std::fmt;
const H:usize=16; const TRAINER:usize=512; const PRG:usize=16384; const CHR:usize=8192;
#[derive(Debug,Clone,Copy,PartialEq,Eq)] pub enum Mirroring{Horizontal,Vertical,FourScreen}
#[derive(Debug,Clone,Copy,PartialEq,Eq)] pub enum HeaderFormat{INes1,Nes2}
#[derive(Debug,Clone,PartialEq,Eq)] pub struct Cartridge<'a>{pub format:HeaderFormat,pub mapper:u16,pub submapper:u8,pub mirroring:Mirroring,pub battery:bool,pub trainer:Option<&'a[u8]>,pub prg_rom:&'a[u8],pub chr_rom:&'a[u8]}
#[derive(Debug,Clone,PartialEq,Eq)] pub enum ParseError{TooSmall,BadMagic,Truncated{needed:usize,actual:usize},UnsupportedNes2RomSizeEncoding}
impl fmt::Display for ParseError{fn fmt(&self,f:&mut fmt::Formatter<'_>)->fmt::Result{match self{
Self::TooSmall=>write!(f,"file is too small to contain an iNES header"),Self::BadMagic=>write!(f,"missing NES\\x1a header magic"),
Self::Truncated{needed,actual}=>write!(f,"ROM is truncated: header describes {needed} bytes, file contains {actual}"),
Self::UnsupportedNes2RomSizeEncoding=>write!(f,"NES 2.0 exponent/multiplier ROM-size encoding is not implemented yet")}}}
impl std::error::Error for ParseError{}
pub fn parse(b:&[u8])->Result<Cartridge<'_>,ParseError>{
 if b.len()<H{return Err(ParseError::TooSmall)} if &b[..4]!=b"NES\x1a"{return Err(ParseError::BadMagic)}
 let f6=b[6];let f7=b[7];let nes2=(f7&0x0c)==0x08;let format=if nes2{HeaderFormat::Nes2}else{HeaderFormat::INes1};
 let mirroring=if f6&8!=0{Mirroring::FourScreen}else if f6&1!=0{Mirroring::Vertical}else{Mirroring::Horizontal};
 let (mapper,submapper,prg_len,chr_len)=if nes2{
  let mapper=((f6 as u16)>>4)|((f7 as u16)&0xf0)|(((b[8]as u16)&0xf)<<8);let sub=b[8]>>4;let pm=b[9]&0xf;let cm=b[9]>>4;
  if pm==0xf||cm==0xf{return Err(ParseError::UnsupportedNes2RomSizeEncoding)}
  (mapper,sub,((((pm as usize)<<8)|b[4]as usize)*PRG),((((cm as usize)<<8)|b[5]as usize)*CHR))
 }else{(((f6 as u16)>>4)|((f7 as u16)&0xf0),0,b[4]as usize*PRG,b[5]as usize*CHR)};
 let mut p=H;let trainer=if f6&4!=0{let e=p+TRAINER;if b.len()<e{return Err(ParseError::Truncated{needed:e,actual:b.len()})}let x=&b[p..e];p=e;Some(x)}else{None};
 let need=p+prg_len+chr_len;if b.len()<need{return Err(ParseError::Truncated{needed:need,actual:b.len()})}
 let prg_rom=&b[p..p+prg_len];p+=prg_len;let chr_rom=&b[p..p+chr_len];
 Ok(Cartridge{format,mapper,submapper,mirroring,battery:f6&2!=0,trainer,prg_rom,chr_rom})
}
#[cfg(test)]mod tests{use super::*;fn rom(p:u8,c:u8,f6:u8)->Vec<u8>{let n=H+(if f6&4!=0{TRAINER}else{0})+p as usize*PRG+c as usize*CHR;let mut r=vec![0;n];r[..4].copy_from_slice(b"NES\x1a");r[4]=p;r[5]=c;r[6]=f6;r}#[test]fn cnrom(){let r=rom(2,2,0x31);let c=parse(&r).unwrap();assert_eq!(c.mapper,3);assert_eq!(c.prg_rom.len(),32768);assert_eq!(c.chr_rom.len(),16384);assert_eq!(c.mirroring,Mirroring::Vertical)}}
