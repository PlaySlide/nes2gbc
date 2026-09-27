#!/usr/bin/env python3
"""Cheaper 6502 carry materialization after a GB subtract/compare.

    ld a, $00 / jr c, :+ / inc a / :      (A = 1 unless GB borrow; 5-6 M)
 -> sbc a / inc a / :                     (same A and C; 2 M)

The GB carry flag afterwards is identical. Z/N/H can differ in the borrow
case (old: flags of the compare; new: Z=1), so a site is rewritten only if a
forward scan proves Z is overwritten before anything could read it (no
conditional Z branch/ret/call, jump, call or return before a Z writer).
The anonymous `:` label is kept so other `:+`/`:-` references still count it.
"""
import re, sys
from pathlib import Path

Z_WRITERS = re.compile(r"^(and|or|xor|cp|sub|sbc|add|adc|inc [abcdehl]$|dec [abcdehl]$|inc \[hl\]|dec \[hl\]|bit|swap|rlca|rrca|rla|rra|rlc|rrc|rl |rr |sla|sra|srl|pop af)")
SAFE = re.compile(r"^(ld |ldh |push |pop (bc|de|hl)|inc (bc|de|hl|sp)|dec (bc|de|hl|sp)|nop|jr n?c,|jp n?c,)")


def code(l):
    return l.split(";", 1)[0].strip()


def z_dead_after(lines, i):
    for j in range(i, min(i + 24, len(lines))):
        s = code(lines[j])
        if not s or s.endswith(":") or s == ":" or s.startswith("IF") or s.startswith("ENDC"):
            if s.startswith("IF") or s.startswith("ENDC") or (s.endswith(":") and not s.startswith(".") and s != ":"):
                return False  # conditional assembly or a global entry: stay conservative
            continue
        if Z_WRITERS.match(s):
            return True
        if s.startswith(("jr c,", "jr nc,", "jp c,", "jp nc,")):
            return False  # taken path not scanned
        if SAFE.match(s):
            continue
        return False
    return False


def main(path):
    p = Path(path)
    lines = p.read_text().splitlines(keepends=True)
    n = skipped = 0
    i = 0
    while i < len(lines) - 4:
        if (code(lines[i]) == "ld a, $00" and code(lines[i + 1]) == "jr c, :+"
                and code(lines[i + 2]) == "inc a" and code(lines[i + 3]) == ":"):
            if z_dead_after(lines, i + 4):
                ind = lines[i][:len(lines[i]) - len(lines[i].lstrip())]
                lines[i:i + 3] = [f"{ind}sbc a ; cheap carry materialize (Z dead)\n", f"{ind}inc a\n"]
                n += 1
            else:
                skipped += 1
        i += 1
    p.write_text("".join(lines))
    print(f"cheap-carry: {n} sites rewritten, {skipped} kept (Z possibly live)")


if __name__ == "__main__":
    main(sys.argv[1])
