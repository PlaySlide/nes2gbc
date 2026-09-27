#!/usr/bin/env python3
"""Instruction-class breakdown of a profile: decodes the ROM byte at every profiled PC."""
import sys, collections, numpy as np
prof = np.load(sys.argv[1]); rom = open(sys.argv[2], "rb").read(); sym = sys.argv[3]
hram = {}
for line in open(sym):
    line = line.split(";")[0].strip()
    if not line: continue
    loc, name = line.split(None, 1); b, a = loc.split(":"); a = int(a, 16)
    if a >= 0xFF80: hram[a] = name
tot = prof.sum(); c = collections.Counter()
for k in np.nonzero(prof)[0]:
    if k >= 0x400000: c["ram-code"] += int(prof[k]); continue
    op = rom[k]
    if op in (0xE0, 0xF0):
        tgt = hram.get(0xFF00 + rom[k + 1], "io/%02X" % rom[k + 1])
        key = ("ldh [%s],a" if op == 0xE0 else "ldh a,[%s]") % tgt
    elif op in (0xC3, 0xC2, 0xCA, 0xD2, 0xDA): key = "jp"
    elif op in (0x18, 0x20, 0x28, 0x30, 0x38): key = "jr"
    elif op in (0xCD, 0xC4, 0xCC): key = "call"
    elif op in (0xC9, 0xD9, 0xC0, 0xC8, 0xD0, 0xD8): key = "ret"
    elif op == 0xFA: key = "ld a,[nn]"
    elif op == 0xEA: key = "ld [nn],a"
    elif op in (0xC5, 0xD5, 0xE5, 0xF5): key = "push"
    elif op in (0xC1, 0xD1, 0xE1, 0xF1): key = "pop"
    else: key = "op %02X" % op
    c[key] += int(prof[k])
for k, v in c.most_common(int(sys.argv[4]) if len(sys.argv) > 4 else 30):
    print(f"{v*100/tot:6.2f}%  {k}")
