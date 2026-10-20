#!/usr/bin/env python3
"""Drop `and a` / `or a` zero tests whose Z flag the host already has.

Translated code often ends a 6502 instruction with a Z test of A right before
a Z branch, although the producing GB instruction already set Z from the same
value:
    dec c / ld a, c / ldh [nes_z_shadow], a / ... / and a / jr nz, nes_T
    sub e / ldh [nes_z_shadow], a / ... / and a / jp z, nes_T
Only flag-neutral instructions (stores of A, register copies from A, pushes,
immediate loads of other registers) may sit between producer and test, with
no label. The test is removed only when the next instruction is a jr/jp on
z/nz to a global label and, on the fall-through path, the next instruction
that touches flags overwrites C (the removed test cleared C/H). A global
label target is a block entry, which never consumes host flags.
NES2GBC_REDUNDANT_ZERO_TEST=0 disables.
"""
import os
import re
import sys
from pathlib import Path

ZA = re.compile(r"^(and|or|xor|sub|add|adc|sbc) (?!hl|sp)|^(inc|dec) a$")
NEUT = re.compile(r"^(ld [bcdehl], a|ldh \[[^\]]+\], a|ld \[[^\]]+\], a|push (af|bc|de|hl)|"
                  r"ld [bcdehl], \$[0-9A-Fa-f]+|ld (bc|de|hl), \$[0-9A-Fa-f]+)$")
CWRITE = re.compile(r"^(add|adc|sub|sbc|and|or|xor|cp|scf|ccf|rla|rra|rlca|rrca|daa_never) |^(scf|ccf|rla|rra|rlca|rrca)$"
                    r"|^(rl|rr|rlc|rrc|sla|sra|srl|swap) ")
BRANCH = re.compile(r"^j[pr] (z|nz), nes_(?:m\d+_b[0-9A-Fa-f]{2}_)?[0-9A-Fa-f]{4}(?:_trace)?$")


def code(l):
    return l.split(";", 1)[0].strip()


def main(path):
    if os.environ.get("NES2GBC_REDUNDANT_ZERO_TEST", "1") == "0":
        print("redundant-zero-test: disabled")
        return
    p = Path(path)
    lines = p.read_text().splitlines(keepends=True)
    idx = [i for i, l in enumerate(lines) if code(l)]
    n_rm = 0
    for k, i in enumerate(idx):
        if code(lines[i]) not in ("and a", "or a") or k == 0 or k + 1 >= len(idx):
            continue
        if not BRANCH.match(code(lines[idx[k + 1]])):
            continue
        # no label between test and branch is guaranteed (labels are code lines)
        j = k - 1
        while j >= 0 and NEUT.match(code(lines[idx[j]])):
            j -= 1
        if j < 0:
            continue
        prod = code(lines[idx[j]])
        ok = bool(ZA.match(prod))
        if not ok:
            m = re.match(r"^ld a, ([bcdehl])$", prod)
            if m:
                j2 = j - 1
                while j2 >= 0 and NEUT.match(code(lines[idx[j2]])) and not code(lines[idx[j2]]).startswith(f"ld {m.group(1)},"):
                    j2 -= 1
                if j2 >= 0 and re.fullmatch(rf"(inc|dec|or|and|xor) {m.group(1)}", code(lines[idx[j2]])):
                    ok = True
        if not ok:
            continue
        # fall-through: next flag-touching instruction must overwrite C
        f = k + 2
        good = False
        while f < len(idx):
            c = code(lines[idx[f]])
            if c == ":":
                f += 1
                continue
            if re.match(r"^nes_(?:m\d+_b[0-9A-Fa-f]{2}_)?[0-9A-Fa-f]{4}(?:_trace)?:$", c) or c.startswith("SECTION"):
                good = True  # falls into a block entry
                break
            if c.endswith(":") or c.startswith(("IF", "ELSE", "ENDC")):
                break
            if re.match(r"^(jp|jr|call|ret|rst)\b", c):
                # unconditional transfer to a block / call: flags not consumed
                good = bool(re.match(r"^j[pr] nes_(?:m\d+_b[0-9A-Fa-f]{2}_)?[0-9A-Fa-f]{4}(?:_trace)?$", c))
                break
            if CWRITE.match(c):
                good = not c.startswith(("adc", "sbc", "rl ", "rr ", "rla", "rra", "ccf"))
                break
            if c.startswith(("inc", "dec", "bit", "res", "set", "cpl", "daa", "pop af", "push af")):
                break
            f += 1
        if not good:
            continue
        ind = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
        lines[i] = f"{ind}; redundant zero test removed: host Z already reflects A\n"
        n_rm += 1
    p.write_text("".join(lines))
    print(f"redundant-zero-test: {n_rm} zero tests removed")


if __name__ == "__main__":
    main(sys.argv[1])
