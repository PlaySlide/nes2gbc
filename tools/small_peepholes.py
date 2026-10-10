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


# Helpers whose inputs are only A/HL (a read/write of NES address HL can never
# consume DE). Whether they clobber or preserve DE, a caller that later reads
# DE sees the same value in both the original and rewritten code only if DE
# is dead, so the scan simply continues through them.
DE_TRANSPARENT_CALLS = {
    "nes_cpu_read", "nes_cpu_read_hi", "nes_cpu_read_hi_view", "nes_cpu_write",
    "nes_prg_read_hi_a", "nes_prg_read_lo_dyn",
}
REG_TOK = re.compile(r"\b(de|d|e)\b")


def de_dead_after(lines, start):
    """True when D and E are both written before any read on every path."""
    work = [(start, frozenset("de"))]
    seen = set()
    steps = 0
    while work:
        i, live = work.pop()
        while True:
            steps += 1
            if steps > 400 or i >= len(lines):
                return False
            if (i, live) in seen:
                break
            seen.add((i, live))
            c = code(lines[i])
            if not live:
                break
            if c == "" or c == ":":
                i += 1
                continue
            if re.match(r"^[A-Za-z_.][\w.]*:", c) or c.startswith(("IF", "ELSE", "ENDC", "PROFILE")):
                return False
            op, _, args = c.partition(" ")
            args = args.strip()
            if op in ("jp", "ret", "reti", "rst", "halt", "stop"):
                return False
            if op == "call":
                if args in DE_TRANSPARENT_CALLS:
                    i += 1
                    continue
                return False
            if op == "jr":
                parts = [x.strip() for x in args.split(",")]
                tgt = parts[-1]
                m = re.fullmatch(r":(\++)", tgt)
                if not m:
                    return False
                n, j = len(m.group(1)), i
                while n:
                    j += 1
                    if j >= len(lines):
                        return False
                    if code(lines[j]) == ":":
                        n -= 1
                    elif re.match(r"^[A-Za-z_.][\w.]*:", code(lines[j])):
                        return False
                work.append((j, live))
                if len(parts) == 1:
                    break
                i += 1
                continue
            if op in ("push", "pop") and args in ("de",):
                if op == "push":
                    return False
                live = frozenset()
                i += 1
                continue
            if op == "ld" and "," in args:
                dst, src = [x.strip() for x in args.split(",", 1)]
                if REG_TOK.search(src):
                    return False
                if "[" in dst:
                    if REG_TOK.search(dst):
                        return False
                elif dst == "de":
                    live = frozenset()
                elif dst in ("d", "e"):
                    live = live - {dst}
                i += 1
                continue
            if op in ("ldh",):
                if REG_TOK.search(args):
                    return False
                i += 1
                continue
            if REG_TOK.search(args):
                return False  # ALU/inc/dec/bit ops reading d/e
            i += 1
    return True


def zp_pointer_via_hl(lines):
    """`ld de,$C0nn / ld a,[de] / ld l,a / inc e / ld a,[de] / ld h,a` ->
    `ld hl,$C0nn / ld a,[hli] / ld h,[hl] / ld l,a` when DE is dead after
    (saves 2 M-cycles; A ends as the high byte as before). nn=$FF wraps inside the zero page and is skipped."""
    n = 0
    pat = ["ld a, [de]", "ld l, a", "inc e", "ld a, [de]", "ld h, a"]
    for i, l in enumerate(lines):
        m = re.match(r"^\s+ld de, \$(C0)([0-9A-Fa-f]{2})\b", l)
        if not m or m.group(2).upper() == "FF":
            continue
        idx, j = [], i + 1
        while len(idx) < 5 and j < len(lines):
            c = code(lines[j])
            if c:
                if c != pat[len(idx)]:
                    break
                idx.append(j)
            elif not lines[j].strip().startswith(";"):
                break
            j += 1
        if len(idx) < 5 or not de_dead_after(lines, idx[-1] + 1):
            continue
        lines[i] = f"    ld hl, ${m.group(1)}{m.group(2)} ; small_peepholes: zp pointer via HL (DE dead)"
        lines[idx[0]] = "    ld a, [hli]"
        lines[idx[1]] = "    ld h, [hl]"
        lines[idx[2]] = "    ld l, a"
        lines[idx[3]] = "    ld a, h ; A = pointer high byte, as before"
        lines[idx[4]] = "    ; (zp pointer via HL)"
        n += 1
    return n


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
    st["zp_ptr_hl"] = zp_pointer_via_hl(lines)
    open(path, "w").write("\n".join(lines))
    print("small_peepholes:", st, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
