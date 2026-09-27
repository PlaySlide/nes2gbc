#!/usr/bin/env python3
"""Map a bench.py --profile histogram onto RGBDS .sym labels."""
import sys, re, bisect, collections, numpy as np
prof = np.load(sys.argv[1]); sym = sys.argv[2]; top = int(sys.argv[3]) if len(sys.argv) > 3 else 40
labels = []
for line in open(sym):
    line = line.split(";")[0].strip()
    if not line: continue
    loc, name = line.split(None, 1)
    b, a = (int(x, 16) for x in loc.split(":"))
    if a < 0x4000: key = a
    elif a < 0x8000: key = b * 0x4000 + a - 0x4000
    else: key = 0x400000 + a
    labels.append((key, name))
labels.sort()
keys = [k for k, _ in labels]
tot = prof.sum()
glob = collections.Counter(); loc = collections.Counter(); cat = collections.Counter()
nz = np.nonzero(prof)[0]
def category(g, k):
    if k >= 0x400000: return "RAM/HRAM code (OAM DMA etc)"
    if re.match(r"nes_[0-9A-F]{4}(_|$)", g): return "translated game code"
    if "isr" in g or g.startswith("nes_video") or g.startswith("nes_gbc"): return "runtime: video/ISR publish"
    if g.startswith("nes_ppu"): return "runtime: PPU reg emulation"
    return "runtime: other helpers"
for k in nz:
    i = bisect.bisect_right(keys, k) - 1
    name = labels[i][1] if i >= 0 else "?"
    g = name.split(".")[0]
    c = int(prof[k])
    glob[g] += c; loc[name] += c; cat[category(g, k)] += c
print(f"total cycles profiled: {tot}")
print("== categories"); [print(f"{c*100/tot:6.2f}%  {n}") for n, c in cat.most_common()]
print("== top global labels"); [print(f"{c*100/tot:6.2f}%  {n}") for n, c in glob.most_common(top)]
print("== top local labels"); [print(f"{c*100/tot:6.2f}%  {n}") for n, c in loc.most_common(top)]
