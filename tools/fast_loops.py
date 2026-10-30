#!/usr/bin/env python3
"""Fast-forward register-only counting self-loops (NES2GBC_FAST_LOOPS).

src/idle.rs::fast_loop_heads lists blocks (runtime/generated.fastloop.txt)
that are `DEX|DEY; BNE self`, `CLC; ADC #n; BCC self` or `SEC; SBC #n; BCS
self`. They touch only one register and the flags, so all iterations but the
last can be run in a tight host loop. This pass inserts that loop right before
the block's first 6502 instruction (after its NMI poll); it leaves the
register at the value the last iteration starts from, and the unchanged block
body then executes that iteration exactly (result, C, Z, N, V). Only the
skipped NMI polls / cycles differ. A block whose body does not start by
loading the register from HRAM is left alone. Opt out: NES2GBC_FAST_LOOPS=0.
"""
import re, sys
from pathlib import Path

asm = Path(sys.argv[1]); lst = asm.with_suffix('.fastloop.txt')
if not lst.exists():
    sys.exit(0)
want = {}
for l in lst.read_text().split('\n'):
    if l.strip():
        lab, k, n = l.split(); want[lab] = (k, int(n))
lines = asm.read_text().split('\n')
reg = {'x': 'nes_x', 'y': 'nes_y', 'adc': 'nes_a', 'sbc': 'nes_a'}
out = []; i = 0; done = 0
while i < len(lines):
    line = lines[i]; out.append(line); i += 1
    m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*):\s*(;.*)?$', line)
    if not m or m.group(1) not in want:
        continue
    lab = m.group(1); k, n = want[lab]
    # find first 6502 instruction comment of this block
    j = i
    while j < len(lines) and not re.match(r'^\s*; \$[0-9A-F]{4}: \$', lines[j]):
        if re.match(r'^[A-Za-z_][A-Za-z0-9_]*:', lines[j]):
            j = None; break
        j += 1
    if j is None or j >= len(lines):
        continue
    # first real instruction after it must load the register from HRAM
    t = j
    while t < len(lines) and (lines[t].strip() == '' or lines[t].strip().startswith(';')):
        t += 1
    if t >= len(lines) or lines[t].split(';')[0].strip() != f'ldh a, [{reg[k]}]':
        continue
    out.extend(lines[i:j]); i = j
    f = f'.ffl_{done}'  # local label: a global one would break the enclosing block's .npoll_* scope
    if k in ('x', 'y'):
        body = [f'    ld a, 1 ; tools/fast_loops.py: DE{k.upper()} loop -> last iteration', f'    ldh [{reg[k]}], a']
    else:
        op = 'add' if k == 'adc' else 'sub'
        body = [f'    ldh a, [nes_a] ; tools/fast_loops.py: {k.upper()} #{n} loop -> last iteration',
                f'{f}:', '    ld d, a', f'    {op} {n}', f'    jr nc, {f}', '    ld a, d', '    ldh [nes_a], a']
    out.extend(body); done += 1
asm.write_text('\n'.join(out))
print(f'fast_loops: {done}/{len(want)} loop(s) fast-forwarded')
