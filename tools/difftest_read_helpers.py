#!/usr/bin/env python3
"""Differential test of the runtime PRG/bus read helpers between two builds.

difftest_read_helpers.py OLD.gbc NEW.gbc NES [N] [SEED]

Loads both ROMs in PyBoy (patched 2.7.0 with a writable register_file), and
for N seeded random cases sets *identical* machine state in both (all WRAM
banks and HRAM random, random PRG/code bank variables, random mapped ROM bank
and SVBK, random registers, random HL over the whole address space with
emphasis on PRG, PRG RAM, $2000-$5FFF registers), then runs one call
sequence in each build from a WRAM stub with interrupts off and compares:
A F B C D E H L SP, mapped ROM bank, SVBK, WRAM0/all WRAMX banks, HRAM
(excluding the stub bytes and the 48 bytes below SP, which only hold dead
stack residue).

Sequences (per case, randomly): the (zp),Y read site as emitted by the
superblock pass,
  OLD build: push bc / [ld a,K+1] / call helper / pop bc
  NEW build: [ld a,K+1] / call helper                 (tools/keep_bc_reads.py)
with helper = nes_cpu_read_hi or nes_cpu_read_hi_view; plus bare calls of
nes_cpu_read_hi / _view / nes_cpu_read (regs after the call, BC not required
equal for the bare calls: compared only on A F D E H L)."""
import sys, random
sys.path.insert(0, "/workspace/perf-bench/src/pyboy-2.7.0")
import logging; logging.disable(logging.WARNING)
from pyboy import PyBoy

old, new, nes = sys.argv[1:4]
N = int(sys.argv[4]) if len(sys.argv) > 4 else 20000
SEED = int(sys.argv[5]) if len(sys.argv) > 5 else 1
nprg = open(nes, "rb").read(16)[4]


def syms(rom):
    d = {}
    for l in open(rom[:-4] + ".sym"):
        p = l.split()
        if len(p) > 1 and ":" in p[0]:
            d[p[1]] = int(p[0].split(":")[1], 16)
    return d


class B:
    def __init__(s, rom):
        s.rom = rom; s.sym = syms(rom)
        s.pb = PyBoy(rom, window="null", cgb=True, sound_emulated=False)
        for _ in range(8): s.pb.tick(1, False)
        s.m = s.pb.memory; s.r = s.pb.register_file
        import io
        s.snap = io.BytesIO(); s.pb.save_state(s.snap)


bo, bn = B(old), B(new)
for k in ("nes_prg_bank", "nes_prg_hi_bank", "nes_prg_fixed_bank", "nes_current_code_bank"):
    assert bo.sym[k] == bn.sym[k], k
STUB = 0xCF00
SP0 = 0xCFF0


def run(b, case, kind, keep_bc_pair):
    b.snap.seek(0); b.pb.load_state(b.snap)   # identical clean CPU/IO state per case
    m, s, r = b.m, b.sym, b.r
    w = bytearray(case["wram"])
    w[s["nes_prg_bank"] - 0xC000] = case["pb"]
    w[s["nes_prg_hi_bank"] - 0xC000] = case["phb"]
    w[s["nes_prg_fixed_bank"] - 0xC000] = case["pfb"]
    callee = {"hi": "nes_cpu_read_hi", "view": "nes_cpu_read_hi_view", "gen": "nes_cpu_read"}[kind]
    ca = s[callee]
    code = [0xF3]
    if keep_bc_pair: code += [0xC5]
    if kind == "view": code += [0x3E, case["a"]]
    code += [0xCD, ca & 0xFF, ca >> 8]
    if keep_bc_pair: code += [0xC1]
    code += [0x18, 0xFE]
    w[STUB - 0xC000:STUB - 0xC000 + len(code)] = bytes(code)
    m[0xC000:0xD000] = bytes(w[:0x1000])
    for bk in range(1, 8): m[bk, 0xD000:0xE000] = bytes(case["wram"][0x1000 * bk:0x1000 * (bk + 1)])
    m[0xFF80:0xFFFF] = bytes(case["hram"])
    m[0xFFFF] = 0   # IE=0: no interrupt can be dispatched
    m[s["nes_current_code_bank"]] = case["ccb"]
    m[0xFF70] = case["svbk"]
    m[0x2000] = case["rombank"]; m[0x3000] = 0
    m[0xFF40] = case["lcdc"]
    r.SP = SP0; r.PC = STUB
    r.A = case["regA"]; r.F = case["f"] & 0xF0
    r.B = case["rb"]; r.C = case["rc"]; r.D = case["rd"]; r.E = case["re"]
    r.HL = case["hl"]
    b.pb.tick(2, False)   # a frame boundary may cut the call; the 2nd frame finishes it
    st = (r.A, r.F, r.B, r.C, r.D, r.E, r.HL >> 8, r.HL & 255, r.SP)
    wm = bytearray(m[0xC000:0xCFFF]) + bytes([m[0xCFFF]])
    for i in range(STUB - 0xC000, STUB - 0xC000 + 0x20): wm[i] = 0
    for i in range(SP0 - 0xC000 - 48, SP0 - 0xC000): wm[i] = 0
    wx = b"".join(bytes(m[bk, 0xD000:0xDFFF]) + bytes([m[bk, 0xDFFF]]) for bk in range(1, 8))
    return st, bytes(wm) + wx, bytes(m[0xFF80:0xFFFF]), bytes(m[0x4000:0x4040]), m[0xFF70]


fails = 0
rng = random.Random(SEED)
kinds = ["hi", "view", "gen"]
for n in range(N):
    kind = kinds[n % 3]
    c = {"seed": rng.getrandbits(32)}
    r2 = random.Random(c["seed"])
    c["wram"] = bytes(r2.getrandbits(8) for _ in range(0x8000))
    c["hram"] = bytes(r2.getrandbits(8) for _ in range(0x7F))
    c["pb"] = r2.randrange(nprg); c["phb"] = r2.randrange(nprg); c["pfb"] = nprg - 1 if r2.random() < .7 else r2.randrange(nprg)
    c["ccb"] = r2.randrange(1, nprg + 1); c["svbk"] = r2.randrange(0, 8); c["rombank"] = r2.randrange(1, nprg + 1)
    c["lcdc"] = r2.choice([0x00, 0x91]); c["a"] = r2.randrange(1, nprg + 1)
    c["regA"] = r2.getrandbits(8); c["f"] = r2.getrandbits(8)
    c["rb"], c["rc"], c["rd"], c["re"] = (r2.getrandbits(8) for _ in range(4))
    t = r2.random()
    if t < 0.35: h = r2.randrange(0x80, 0x100)
    elif t < 0.55: h = r2.randrange(0x60, 0x80)
    elif t < 0.75: h = r2.randrange(0x20, 0x60)
    elif t < 0.82: h = 0x40
    else: h = r2.randrange(0, 0x20)
    l = r2.getrandbits(8)
    if h == 0x40 and r2.random() < 0.8: l = r2.choice([0x11, 0x15, 0x16, 0x17, 0x00, 0x18, 0x14])
    if 0x20 <= h < 0x40 and r2.random() < 0.7: l = r2.choice([2, 4, 7, 0x0A, 0x0F])
    c["hl"] = h << 8 | l
    if kind == "view": c["a"] = c["pb"] + 1 if r2.random() < 0.5 else c["a"]
    ro = run(bo, c, kind, kind != "gen")
    rn = run(bn, c, kind, False)
    # hi/view sequence compares everything; gen call compares everything but B,C
    so, sn = list(ro[0]), list(rn[0])
    if kind == "gen":
        so[2:4] = [0, 0]; sn[2:4] = [0, 0]
    ok = so == sn and ro[1:] == rn[1:] if kind != "gen" else (so == sn and ro[1:] == rn[1:])
    if not ok:
        fails += 1
        if fails <= 10:
            print("MISMATCH", kind, "hl=%04X" % c["hl"], "A=%02X" % c["a"], "old", ro[0], "new", rn[0],
                  "mem" if ro[1] != rn[1] else "", "hram" if ro[2] != rn[2] else "", "bank", ro[3], rn[3], "svbk", ro[4], rn[4])
    if (n + 1) % 5000 == 0: print("..", n + 1, "cases, mismatches", fails, flush=True)
print(f"difftest {old} vs {new}: {N} cases, {fails} mismatches")
sys.exit(1 if fails else 0)
