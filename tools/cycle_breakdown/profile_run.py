#!/usr/bin/env python3
"""Run a ROM in PyBoy (sound emulated) and record GB cycles per executed
instruction address over NES frames A..B.

usage: profile_run.py ROM.gbc NES.nes FRAMES PRESS A B OUT.npy
  FRAMES: 'nmi' counts NES frames by hooking the translated NMI vector block;
          a hex WRAM offset counts them by a game frame counter at $C000+off.
  PRESS : 'zelda' (scripted Zelda title/file-select, then right/up walking),
          or 'btn@frame:len,...' keyed to NES frames.
Needs the PyBoy build with prof_attach (perf-bench/src/pyboy-2.7.0): it adds
the T-cycles of each executed instruction to prof[key], key = PC (ROM0),
bank*$4000+PC-$4000 (ROMX) or $400000+PC (RAM).  Writes OUT.npy and
OUT.json (NES frames, GB frames, double speed)."""
import sys, os, json
sys.path.insert(0, "/workspace/perf-bench/src/pyboy-2.7.0")
import logging; logging.disable(logging.WARNING)
from pyboy import PyBoy
import numpy as np
rom, nes, frames, press, A, B, out = sys.argv[1:8]; A = int(A); B = int(B)
sy = {}
for l in open(rom[:-4] + '.sym'):
    p = l.split()
    if len(p) > 1 and ':' in p[0]:
        b, o = p[0].split(':'); sy[p[1]] = (int(b, 16), int(o, 16))
pb = PyBoy(rom, window="null", cgb=True, sound_emulated=True); m = pb.memory
prof = np.zeros(0x410000, dtype=np.uint64)
cnt = [0]
if frames == 'nmi':
    d = open(nes, 'rb').read(); prg = d[16:16 + d[4] * 0x4000]; nv = prg[-6] | prg[-5] << 8
    def h(_): cnt[0] += 1
    pb.hook_register(*sy['nes_%04X' % nv], h, None)
    FC = None
else:
    FC = int(frames, 16)
pr = []
for p in filter(None, '' if press == 'zelda' else press.split(',')):
    btn, rest = p.split('@'); f, dd = rest.split(':'); pr.append((btn, int(f), int(dd)))
ZEL = press == 'zelda'
if ZEL:
    sys.path.insert(0, '/workspace/roms-compat/m12'); from zplan import zelda_buttons
    zs = {}; zt = 0; zl = None; zfc = None; zw = set(); play = False
nf = 0; held = set(); gbf = 0; fa = None; last = m[0xC000 + FC] if FC is not None else 0; lastc = 0
ds = 0
while nf < B:
    if ZEL:
        mode, sub, fcv = m[0xC012], m[0xC013], m[0xC015]
        if mode != zl: zl = mode; zt = 0
        if fcv != zfc:
            zfc = fcv
            if not play and mode != 5: zw = zelda_buttons(mode, sub, zt, zs); zt += 1
            else:
                if not play: play = True; nf = 0; last = fcv
                zw = {'right'} if (nf // 40) % 2 == 0 else {'up'}
        if not play:
            for b in zw - held: pb.button_press(b)
            for b in held - zw: pb.button_release(b)
            held = zw; pb.tick(1, False); gbf += 1; last = m[0xC000 + FC]; continue
        w = zw
    else:
        w = {b for b, s, dd in pr if s <= nf < s + dd}
    for b in w - held: pb.button_press(b)
    for b in held - w: pb.button_release(b)
    held = w; pb.tick(1, False); gbf += 1
    if m[0xFFFE] == 1: print("FAULT at", nf); break
    if FC is None:
        d = cnt[0] - lastc; lastc = cnt[0]
    else:
        v = m[0xC000 + FC]; d = (v - last) & 255; last = v
        if d > 4: d = 1
    for k in range(d):
        nf += 1
        if nf == A: fa = gbf; pb.prof_attach(prof); ds = m[0xFF4D] >> 7
pb.prof_detach()
np.save(out, prof)
meta = dict(nes_frames=B - A, gb_frames=gbf - fa, double_speed=int(ds),
            executed_cycles=int(prof.sum()))
json.dump(meta, open(out[:-4] + '.json', 'w'))
print(json.dumps(meta))
