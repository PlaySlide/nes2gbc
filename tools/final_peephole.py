#!/usr/bin/env python3
"""Last-pass redundant-load elimination on straight-line generated code.

Tracks the set of places (nes_* HRAM state bytes, 8-bit registers) known to
equal A. `ldh a, [nes_X]` / `ld a, r` whose source is in the set is dropped
(loads do not affect flags). The set is updated conservatively: labels,
IF/ELSE/ENDC, any control transfer, and unrecognised instructions clear it;
indirect stores clear the HRAM part; writes to A or to a register remove it.
Only the translated-CPU state bytes (STATE) are tracked; ISRs never write them.
"""
import re, sys
from pathlib import Path

REGS = set("bcdehl")
# Only translated-CPU state bytes: written solely by translated code (the
# NMI entry is a synchronous call). Other nes_* HRAM bytes (e.g.
# nes_host_vblank_pending, pacing/split state) are written by the ISRs.
STATE = {"nes_a", "nes_x", "nes_y", "nes_sp", "nes_p", "nes_z_shadow", "nes_n_shadow", "nes_c_shadow"}
PAIRS = {"bc": "bc", "de": "de", "hl": "hl", "af": "a"}
ALU_A = {"and", "or", "xor", "add", "adc", "sub", "sbc"}
ROT_A = {"rla", "rra", "rlca", "rrca", "cpl", "daa"}
CB = {"rl", "rr", "rlc", "rrc", "sla", "sra", "srl", "swap", "set", "res"}
FLAGONLY = {"scf", "ccf", "nop", "cp", "bit", "PROFILE_INC", "di", "ei"}


def code(l):
    return l.split(";", 1)[0].strip()


def step(c, S):
    """Return (new_set, removable)."""
    op, _, rest = c.partition(" ")
    args = [x.strip() for x in rest.split(",")] if rest else []
    if op == "ldh" and len(args) == 2:
        d, s = args
        if s == "a" and d[1:-1] in STATE:
            return S | {d[1:-1]}, False
        if d == "a" and s[1:-1] in STATE:
            v = s[1:-1]
            return ({v} | {x for x in S if x in REGS}) if v in S else {v}, v in S
        if d == "a":
            return set(), False
        if s == "a":  # ldh [io or c], a : may alias nothing we track except via [c]
            return {x for x in S if x in REGS} if d in ("[c]", "[$ff00+c]") else S, False
        return set(), False
    if op == "ld" and len(args) == 2:
        d, s = args
        if d == "a":
            if s in REGS:
                return (S if s in S else {s}), s in S
            if re.fullmatch(r"\$[0-9A-Fa-f]{1,2}", s):
                k = "#%02X" % int(s[1:], 16)
                return (S if k in S else {k}), k in S
            return set(), False
        if d in REGS:
            S2 = S - {d}
            return (S2 | {d}) if s == "a" else S2, False
        if d in ("bc", "de", "hl", "sp"):
            return S - set(d), False
        if d.startswith("["):  # memory store: may alias HRAM through a pointer
            S2 = {x for x in S if x in REGS}
            if d in ("[hli]", "[hld]", "[hl+]", "[hl-]"):
                S2 -= {"h", "l"}
            return S2, False
        return set(), False
    if op in ("inc", "dec") and args:
        r = args[0]
        if r == "a":
            return set(), False
        if r in REGS:
            return S - {r}, False
        if r in PAIRS:
            return S - set(r), False
        return {x for x in S if x in REGS}, False  # inc [hl]
    if op == "xor" and args == ["a"]:
        return {"#00"}, False
    if op in ALU_A or op in ROT_A:
        return set(), False
    if op in CB and args:
        r = args[-1]
        if r == "a":
            return set(), False
        if r in REGS:
            return S - {r}, False
        return {x for x in S if x in REGS}, False
    if op == "push":
        return S, False
    if op == "pop" and args:
        return (set() if args[0] == "af" else S - set(args[0])), False
    if op == "add" and args and args[0] == "hl":
        return S - {"h", "l"}, False
    if op in FLAGONLY:
        return S, False
    return set(), False


def main(path):
    p = Path(path)
    lines = p.read_text().splitlines(keepends=True)
    S = set()
    n = 0
    for i, l in enumerate(lines):
        c = code(l)
        if not c:
            continue
        if c.endswith(":") or c.startswith(("IF", "ELSE", "ENDC", "SECTION", "ELIF")) or c.startswith("."):
            S = set()
            continue
        S, rm = step(c, S)
        if rm:
            lines[i] = "    ; redundant load removed (final peephole)\n"
            n += 1
    p.write_text("".join(lines))
    print(f"final-peephole: {n} redundant loads removed")


if __name__ == "__main__":
    main(sys.argv[1])
