#!/usr/bin/env python3
"""Use B/C as short-lived residual caches between native B/C uses.

The validated whole-block B/C caches run first and retain priority. A register
already holding one of those long-lived caches is reserved for the entire block.
For remaining registers, native B/C uses, helper calls, and local host control
flow are barriers. Within each straight-line window the register may cache a
profitable remaining X/Y/A or direct zero-page value. Canonical state remains
authoritative and stores are preserved/mirrored.
"""
from __future__ import annotations
import argparse, re
from dataclasses import dataclass
from pathlib import Path
BLOCK_RE=re.compile(r"^nes_([0-9A-Fa-f]{4}):$")
ZP_LOAD_RE=re.compile(r"ld a, \[\$C0([0-9A-Fa-f]{2})\]$",re.I)
ZP_STORE_RE=re.compile(r"ld \[\$C0([0-9A-Fa-f]{2})\], ([a-z0-9$]+)$",re.I)
INDIRECT_MEM_RE=re.compile(r"\[(?:hl|de|bc|hli|hld)\]",re.I)
CACHED_INDEX_RE=re.compile(r"ld a, ([bc])\s*;\s*BC window cache nes_([xy])$",re.I)
PAGE_BASE_RE=re.compile(r"ld hl, \$[0-9A-Fa-f]{2}00$",re.I)
def code(line): return line.split(';',1)[0].strip()
def comment(line):
 p=line.split(';',1); return p[1].strip().lower() if len(p)==2 else ''
def indent_of(line): return line[:len(line)-len(line.lstrip())]
def normalize(lines): lines[:]=''.join(lines).splitlines(keepends=True)
def next_code_index(lines,start,ceiling=None):
 end=len(lines) if ceiling is None else min(ceiling,len(lines))
 for i in range(start,end):
  if code(lines[i]): return i
 return None
def prev_code_index(lines,start,floor=0):
 for i in range(start,floor-1,-1):
  if code(lines[i]): return i
 return None
@dataclass
class Block: start:int; end:int; addr:int
def blocks(lines):
 labels=[]
 for i,line in enumerate(lines):
  m=BLOCK_RE.fullmatch(code(line))
  if m: labels.append((i,int(m.group(1),16)))
 out=[]
 for n,(start,addr) in enumerate(labels):
  end=labels[n+1][0] if n+1<len(labels) else len(lines)
  for j in range(start+1,end):
   if code(lines[j]).startswith('SECTION '): end=j; break
  out.append(Block(start,end,addr))
 return out
def line_uses_reg(c,reg):
 return bool(c and (re.search(r'\bbc\b',c,re.I) or re.search(rf'\b{reg}\b',c,re.I)))
def is_control_barrier(c):
 if not c:return False
 return c.endswith(':') or c.lower().startswith(('call ','jr ','jp ','ret','reti'))
def is_barrier(line,reg):
 c=code(line); return is_control_barrier(c) or line_uses_reg(c,reg)
def has_long_lived_cache(body,reg):
 needles=('cached nes_','seed nes_','refresh nes_','cached nes zp','seed nes zp','refresh nes zp','cached 6502 a','seed 6502 a','refresh 6502 a','cached x/y')
 for line in body:
  if line_uses_reg(code(line),reg) and any(n in comment(line) for n in needles): return True
 return False
def state_access(line,state):
 c=code(line).lower(); s=state.lower()
 if c in {f'ldh a, [{s}]',f'ld a, [{s}]'}: return 'load'
 if c in {f'ldh [{s}], a',f'ld [{s}], a'}: return 'store'
 return None
def state_stats(body,state):
 loads=stores=0; first=None
 for line in body:
  k=state_access(line,state)
  if k is None: continue
  if first is None:first=k
  if k=='load':loads+=1
  else:stores+=1
 return loads,stores,first
def hram_gain(loads,stores,first):
 if first=='load':return 2*loads-3-stores
 if first=='store':return 2*loads-stores
 return -1
def zp_stats(body):
 loads={};stores={};bad=set();first={}
 for line in body:
  c=code(line);m=ZP_LOAD_RE.fullmatch(c)
  if m:
   a=int(m.group(1),16);first.setdefault(a,'load');loads[a]=loads.get(a,0)+1;continue
  m=ZP_STORE_RE.fullmatch(c)
  if m:
   a=int(m.group(1),16);first.setdefault(a,'store')
   if m.group(2).lower()=='a':stores[a]=stores.get(a,0)+1
   else:bad.add(a)
 keys=set(loads)|set(stores)|bad
 return {a:(loads.get(a,0),stores.get(a,0),a in bad,first.get(a)) for a in keys}
def zp_gain(loads,stores,first):
 if first=='load':return 3*loads-4-stores
 if first=='store':return 3*loads-stores
 return -1
@dataclass
class Stats:
 windows:int=0;state_values:int=0;zp_values:int=0;replaced_loads:int=0;mirrored_stores:int=0;load_seeds:int=0;store_seeds:int=0;reserved_blocks:int=0
def optimize_window(lines,start,end,reg,s):
 if end<=start:return
 body=lines[start:end];cands=[]
 for state in ('nes_x','nes_y','nes_a'):
  l,st,f=state_stats(body,state);g=hram_gain(l,st,f)
  if g>0:cands.append((g,'state',state))
 if not any(INDIRECT_MEM_RE.search(code(x)) for x in body):
  for a,(l,st,bad,f) in zp_stats(body).items():
   if bad or l==0:continue
   g=zp_gain(l,st,f)
   if g>0:cands.append((g,'zp',a))
 if not cands:return
 cands.sort(key=lambda x:x[0],reverse=True);_,kind,key=cands[0];init=False
 for i in range(start,end):
  ind=indent_of(lines[i])
  if kind=='state':
   k=state_access(lines[i],key)
   if k=='load':
    if init:lines[i]=f'{ind}ld a, {reg} ; BC window cache {key}\n';s.replaced_loads+=1
    else:lines[i]+=f'{ind}ld {reg}, a ; seed BC window cache {key}\n';init=True;s.load_seeds+=1
   elif k=='store':
    was=init;lines[i]+=f'{ind}ld {reg}, a ; refresh BC window cache {key}\n';s.mirrored_stores+=1;init=True
    if not was:s.store_seeds+=1
  else:
   c=code(lines[i]);m=ZP_LOAD_RE.fullmatch(c)
   if m and int(m.group(1),16)==key:
    if init:lines[i]=f'{ind}ld a, {reg} ; BC window cache NES ZP ${key:02X}\n';s.replaced_loads+=1
    else:lines[i]+=f'{ind}ld {reg}, a ; seed BC window cache NES ZP ${key:02X}\n';init=True;s.load_seeds+=1
    continue
   m=ZP_STORE_RE.fullmatch(c)
   if m and int(m.group(1),16)==key and m.group(2).lower()=='a':
    was=init;lines[i]+=f'{ind}ld {reg}, a ; refresh BC window cache NES ZP ${key:02X}\n';s.mirrored_stores+=1;init=True
    if not was:s.store_seeds+=1
 s.windows+=1
 if kind=='state':s.state_values+=1
 else:s.zp_values+=1
def optimize_reg(lines,reg,s):
 for b in reversed(blocks(lines)):
  body=lines[b.start+1:b.end]
  if has_long_lived_cache(body,reg):s.reserved_blocks+=1;continue
  ws=b.start+1
  for i in range(b.start+1,b.end):
   if is_barrier(lines[i],reg):optimize_window(lines,ws,i,reg,s);ws=i+1
  optimize_window(lines,ws,b.end,reg,s)
def fold_direct_cached_indexes(lines):
 prg=page=zp0=0
 for i,line in enumerate(lines):
  m=CACHED_INDEX_RE.fullmatch(line.strip())
  if not m:continue
  reg=m.group(1).lower();j=next_code_index(lines,i+1,min(len(lines),i+10))
  if j is None or code(lines[j])!='ld l, a':continue
  kind=None
  if '256-byte-aligned table: low byte is index' in lines[j]:
   k=next_code_index(lines,j+1,min(len(lines),j+5))
   if k is not None and code(lines[k])=='ld a, [hl]':kind='prg'
  if kind is None and any('dead host-flag scaffold removed: zero-page $00 index' in lines[k] for k in range(i+1,j)):kind='zp0'
  if kind is None:
   p=prev_code_index(lines,i-1,max(0,i-4));mark=any('dead host-flag scaffold removed: page-aligned index' in lines[k] for k in range(j+1,min(len(lines),j+6)))
   if p is not None and PAGE_BASE_RE.fullmatch(code(lines[p])) and mark:kind='page'
  if kind is None:continue
  ind=indent_of(lines[i]);lines[i]=f'{ind}; BC window cached index moved directly into L\n'
  if kind=='prg':lines[j]=f'{ind}ld l, {reg} ; BC-window X/Y + aligned PRG table\n';prg+=1
  elif kind=='page':lines[j]=f'{ind}ld l, {reg} ; BC-window X/Y + page-aligned RAM base\n';page+=1
  else:lines[j]=f'{ind}ld l, {reg} ; BC-window X/Y + zero-page $00 base\n';zp0+=1
 return prg,page,zp0
def optimize(lines):
 s=Stats();optimize_reg(lines,'b',s);normalize(lines);optimize_reg(lines,'c',s);normalize(lines);p,q,r=fold_direct_cached_indexes(lines);return s,p,q,r
def main():
 p=argparse.ArgumentParser();p.add_argument('asm',type=Path);a=p.parse_args();lines=a.asm.read_text(encoding='utf-8').splitlines(keepends=True);s,prg,page,zp0=optimize(lines);a.asm.write_text(''.join(lines),encoding='utf-8');print(f'bc-window: cached {s.state_values} CPU-state + {s.zp_values} ZP value(s) across {s.windows} straight-line window(s), replaced {s.replaced_loads} loads, mirrored {s.mirrored_stores} stores; seeded {s.load_seeds} on first load / {s.store_seeds} from prior store; reserved {s.reserved_blocks} long-lived-cache register/block pair(s); fed {prg}/{page}/{zp0} cached X/Y index(es) directly into PRG/page/ZP00 addresses');return 0
if __name__=='__main__':raise SystemExit(main())
