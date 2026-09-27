#!/usr/bin/env python3
"""Generic determinism check for any nes2gbc build: NES RAM hash at fixed NMI indices
with NMI-indexed input (Start @60-65, then A/Right pulses). Also reports work-free %native."""
import sys, os, hashlib
import os
sys.path.insert(0, os.environ.get("PYBOY_PROF_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), ".pyboy", "pyboy-2.7.0")))
import logging; logging.disable(logging.WARNING)
from pyboy import PyBoy
from pyboy.utils import WindowEvent as W
nes, gbc = sys.argv[1], sys.argv[2]; N = int(sys.argv[3]) if len(sys.argv) > 3 else 600
prg = open(nes, "rb").read()
banks = prg[4]; vec = prg[16 + banks * 16384 - 6: 16 + banks * 16384 - 4]
nmi = vec[0] | vec[1] << 8
syms = {}
for l in open(os.path.splitext(gbc)[0] + ".sym"):
    l = l.split(";")[0].strip()
    if l: loc, name = l.split(None, 1); b, a = loc.split(":"); syms[name] = (int(b, 16), int(a, 16))
pb = PyBoy(gbc, window="null", cgb=True, sound_emulated=False)
st = {"n": 0, "p": set(), "h": {}}
BTN = {"start": (W.PRESS_BUTTON_START, W.RELEASE_BUTTON_START), "a": (W.PRESS_BUTTON_A, W.RELEASE_BUTTON_A),
       "right": (W.PRESS_ARROW_RIGHT, W.RELEASE_ARROW_RIGHT)}
def want(n):
    s = set()
    if 60 <= n < 66: s.add("start")
    if n >= 150 and (n // 40) % 2: s.add("right")
    if n >= 150 and n % 50 < 8: s.add("a")
    return s
def on_nmi(_):
    st["n"] += 1
    if st["n"] % 50 == 0: st["h"][st["n"]] = hashlib.sha1(bytes(pb.memory[0xC000:0xC800])).hexdigest()[:8]
def on_latch(_):
    w = want(st["n"])
    for b in w - st["p"]: pb.button_event_now(W(BTN[b][0]))
    for b in st["p"] - w: pb.button_event_now(W(BTN[b][1]))
    st["p"] = w
b, a = syms["nes_%04X" % nmi]; pb.hook_register(b, a, on_nmi, None)
b, a = syms["nes_controller_latch"]; pb.hook_register(b, a, on_latch, None)
t = 0
while st["n"] < N and t < N * 8: pb.tick(1, False); t += 1
print(f"{os.path.basename(nes)} nmi=${nmi:04X} nmis={st['n']} ticks={t} %native={100*st['n']/t:.1f}")
print(" ".join(f"{k}:{v}" for k, v in st["h"].items()))
