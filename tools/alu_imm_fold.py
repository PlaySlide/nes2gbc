#!/usr/bin/env python3
"""Fold known-carry ADC and register-immediate ALU operands (late pass).

1. CLC + ADC:   and a ; known 6502 C=0     ->  (removed)
                ld a, d                         ld a, d
                adc e                           add e
   AND A leaves A unchanged and only defines flags, LD touches none, and ADC
   with C=0 produces exactly ADD's result and flags.

2. Immediate operand:  ld e, $30 / ... / add e  ->  ... / add $30
   when E is not touched in between and is dead after the ALU op
   (tools/gbregs.Liveness), saving the 2 M load for a 1 M wider operand.

Only blank/comment lines and the instructions named above may sit between
the pair; labels, control flow and IF/ENDC blocks stop the match.
"""
import re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gbregs import Liveness, code, effect, if_depth, is_label, split  # noqa: E402

ALU = ("add", "adc", "sub", "sbc", "and", "or", "xor", "cp")


def nxt(codes, j):
    while j < len(codes) and not codes[j]:
        j += 1
    return j


def main(path):
    p = Path(path)
    lines = p.read_text().splitlines(keepends=True)
    codes = [code(l) for l in lines]
    depth = if_depth(codes)
    live = Liveness(lines, codes)

    def repl(i, new):
        ind = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
        lines[i] = f"{ind}{new}\n" if new else f"{ind}; folded: {codes[i]}\n"
        codes[i] = new.split(";", 1)[0].strip() if new else ""

    clc = imm = 0
    for i, c in enumerate(codes):
        if depth[i] or c != "and a":
            continue
        j = nxt(codes, i + 1)
        while j < len(codes) and re.match(r"^ld [abcdehl], [abcdehl]$", codes[j]) and not depth[j]:
            j = nxt(codes, j + 1)
        if j < len(codes) and not depth[j] and re.match(r"^adc (a, )?\S+$", codes[j]):
            op, a = split(codes[j])
            repl(j, f"add {a[-1].upper() if a[-1].startswith('$') else a[-1]} ; ADC after CLC (C=0)")
            repl(i, "")
            clc += 1
    for i, c in enumerate(codes):
        m = re.match(r"^ld ([bcdehl]), (\$[0-9A-Fa-f]{1,2})$", c)
        if depth[i] or not m:
            continue
        r, n = m.group(1), m.group(2)
        j = nxt(codes, i + 1)
        while j < len(codes) and not depth[j]:
            cj = codes[j]
            if is_label(cj):
                break
            mm = re.match(r"^(add|adc|sub|sbc|and|or|xor|cp) (?:a, )?([bcdehl])$", cj)
            if mm and mm.group(2) == r:
                if live.dead_after(j, {r}):
                    repl(j, f"{mm.group(1)} {n} ; immediate folded from {r.upper()}")
                    repl(i, "")
                    imm += 1
                break
            e = effect(cj)
            if e is None or r in (e[0] | e[1]):
                break
            j = nxt(codes, j + 1)
    p.write_text("".join(lines))
    print(f"alu-imm-fold: {clc} CLC+ADC -> ADD, {imm} register operands -> immediates")


if __name__ == "__main__":
    main(sys.argv[1])
