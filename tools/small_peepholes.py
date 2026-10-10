#!/usr/bin/env python3
"""Exact local peepholes on the final generated asm.

* `ld e, $lo / ld d, $hi` (also l/h, c/b) -> `ld de, $hilo` (1 byte, 1 M-cycle)
* `push af / ld rr, $imm / pop af` -> `ld rr, $imm` (16-bit immediate loads
  touch neither A nor flags)
* `ld r, a / ld a, r` -> drop the copy back (A already equals r)

Only adjacent code lines are paired; comment-only lines may sit between them,
labels (named or anonymous) may not. NES2GBC_SMALL_PEEPHOLES=0 disables.
"""
import os
import re
import sys

PAIRS = {("e", "d"): "de", ("l", "h"): "hl", ("c", "b"): "bc"}
IMM8 = re.compile(r"^\s+ld ([bcdehl]), \$([0-9A-Fa-f]{2})\s*(;.*)?$")
IMM16 = re.compile(r"^\s+ld (hl|de|bc), \$[0-9A-Fa-f]{4}\s*(;.*)?$")
COPY = re.compile(r"^\s+ld ([bcdehl]), a\s*(;.*)?$")


def code(l):
    return l.split(";", 1)[0].strip()


def main() -> int:
    path = sys.argv[1]
    if os.environ.get("NES2GBC_SMALL_PEEPHOLES", "1") == "0":
        return 0
    lines = open(path).read().split("\n")

    def nxt(i):
        j = i + 1
        while j < len(lines) and code(lines[j]) == "" and lines[j].strip().startswith(";"):
            j += 1
        return j

    st = {"imm16": 0, "pushpop": 0, "copyback": 0}
    i = 0
    while i < len(lines):
        if not code(lines[i]):
            i += 1
            continue
        j = nxt(i)
        if j >= len(lines):
            break
        a, b = IMM8.match(lines[i]), IMM8.match(lines[j])
        if a and b and (a.group(1), b.group(1)) in PAIRS:
            rr = PAIRS[(a.group(1), b.group(1))]
            lines[i] = f"    ld {rr}, ${b.group(2)}{a.group(2)} ; small_peepholes: paired imm"
            lines[j] = "    ; (paired into ld %s)" % rr
            st["imm16"] += 1
            i = j + 1
            continue
        if code(lines[i]) == "push af" and IMM16.match(lines[j]):
            k = nxt(j)
            if k < len(lines) and code(lines[k]) == "pop af":
                lines[i] = "    ; push af dropped (small_peepholes)"
                lines[k] = "    ; pop af dropped (small_peepholes)"
                st["pushpop"] += 1
                i = k + 1
                continue
        c = COPY.match(lines[i])
        if c and code(lines[j]) == f"ld a, {c.group(1)}":
            lines[j] = "    ; copy-back removed (small_peepholes)"
            st["copyback"] += 1
            i = j + 1
            continue
        i += 1
    open(path, "w").write("\n".join(lines))
    print("small_peepholes:", st, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
