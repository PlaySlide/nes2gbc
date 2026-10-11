#!/usr/bin/env python3
"""Flag-liveness-guarded peepholes on the final generated asm.

* `ld a, $00` -> `xor a` when host Z and C are dead afterwards (1 byte, 1 M).
* `ld a, $00 / rl a|rla / ldh [nes_c_shadow], a` (carry capture as 0/1) ->
  `sbc a / ldh [nes_c_shadow], a` (capture as 0/$FF, the form the other carry
  captures already use) when A, Z and C are dead after the store.

A path-sensitive forward scan follows fallthrough and anonymous `:+` jumps;
anything it cannot follow (named labels, jp/ret/call, backward jumps,
conditional assembly) counts as a read, so a rewrite only happens when every
path redefines the tracked state first. NES2GBC_FLAG_DEAD_PEEPHOLES=0 disables.
"""
import os
import re
import sys

ZC_WRITERS = {"add", "adc", "sub", "sbc", "and", "or", "xor", "cp", "rla", "rra",
              "rlca", "rrca", "rl", "rr", "rlc", "rrc", "sla", "sra", "srl", "scf"}
# scf sets C only; treat specially below.
C_READERS = {"adc", "sbc", "rla", "rra", "rl", "rr", "ccf"}
A_SRC_OPS = {"add", "adc", "sub", "sbc", "and", "or", "xor", "cp"}
LABEL = re.compile(r"^[A-Za-z_.][\w.]*:")


def code(l):
    return l.split(";", 1)[0].strip()


def dead_after(lines, start, track):
    """True if every path from `start` writes each tracked item ('a','z','c')
    before reading it."""
    work = [(start, frozenset(track))]
    seen = set()
    steps = 0
    while work:
        i, live = work.pop()
        while live:
            steps += 1
            if steps > 600 or i >= len(lines):
                return False
            if (i, live) in seen:
                break
            seen.add((i, live))
            c = code(lines[i])
            if c == "" or c == ":" or c.startswith("PROFILE_INC"):
                i += 1
                continue
            if LABEL.match(c) or c.startswith(("IF", "ELSE", "ENDC")):
                return False
            op, _, args = c.partition(" ")
            args = args.strip()
            parts = [x.strip() for x in args.split(",")] if args else []
            if op in ("jp", "ret", "reti", "rst", "call", "halt", "stop", "daa", "di", "ei"):
                return False
            if op == "push":
                if args == "af":
                    return False
                i += 1
                continue
            if op == "pop":
                if args == "af":
                    live = frozenset()
                i += 1
                continue
            if op == "jr":
                cond = parts[0] if len(parts) == 2 else None
                if cond in ("z", "nz") and "z" in live:
                    return False
                if cond in ("c", "nc") and "c" in live:
                    return False
                m = re.fullmatch(r":(\++)", parts[-1])
                if not m:
                    return False
                n, j = len(m.group(1)), i
                while n:
                    j += 1
                    if j >= len(lines):
                        return False
                    cj = code(lines[j])
                    if cj == ":":
                        n -= 1
                    elif LABEL.match(cj):
                        return False
                work.append((j, live))
                if cond is None:
                    break
                i += 1
                continue
            reads_a = writes_a = False
            reads_c = writes_c = writes_z = False
            if op in ("ld", "ldh"):
                dst, src = parts[0], parts[1]
                if re.search(r"\ba\b", src) or src in ("[hli]", "[hld]") and False:
                    reads_a = True
                if src == "a":
                    reads_a = True
                if dst == "a":
                    writes_a = True
                if re.search(r"\ba\b", dst) and dst != "a":
                    reads_a = True
            elif op in A_SRC_OPS:
                reads_a = True
                writes_a = op != "cp"
                writes_z = writes_c = True
                if op in ("adc", "sbc"):
                    reads_c = True
                if op in ("xor", "sub") and parts == ["a"]:
                    reads_a = False
            elif op in ("inc", "dec"):
                if args == "a":
                    reads_a = writes_a = True
                if len(args) == 1 or args == "[hl]":
                    writes_z = True
            elif op in ("rla", "rra", "rlca", "rrca"):
                reads_a = writes_a = True
                writes_c = writes_z = True
                reads_c = op in ("rla", "rra")
            elif op in ("rl", "rr", "rlc", "rrc", "sla", "sra", "srl", "swap"):
                writes_z = True
                writes_c = op != "swap"
                reads_c = op in ("rl", "rr")
                if args == "a":
                    reads_a = writes_a = True
                if op == "swap":
                    writes_c = True
            elif op == "bit":
                writes_z = True
                if parts[-1] == "a":
                    reads_a = True
            elif op in ("res", "set"):
                if parts[-1] == "a":
                    reads_a = writes_a = True
            elif op == "cpl":
                reads_a = writes_a = True
            elif op == "scf":
                writes_c = True
            elif op == "ccf":
                reads_c = writes_c = True
            elif op == "nop":
                pass
            else:
                return False
            if reads_a and "a" in live:
                return False
            if reads_c and "c" in live:
                return False
            if writes_a:
                live = live - {"a"}
            if writes_c:
                live = live - {"c"}
            if writes_z:
                live = live - {"z"}
            i += 1
    return True


def main() -> int:
    path = sys.argv[1]
    if os.environ.get("NES2GBC_FLAG_DEAD_PEEPHOLES", "1") == "0":
        return 0
    lines = open(path).read().split("\n")
    nx = nc = 0
    for i, l in enumerate(lines):
        if not re.match(r"^\s+ld a, \$00\s*(;.*)?$", l):
            continue
        # carry capture form
        j = i + 1
        while j < len(lines) and code(lines[j]) == "" and lines[j].strip().startswith(";"):
            j += 1
        k = j + 1
        while k < len(lines) and code(lines[k]) == "" and lines[k].strip().startswith(";"):
            k += 1
        if (j < len(lines) and code(lines[j]) in ("rla", "rl a") and k < len(lines)
                and code(lines[k]) == "ldh [nes_c_shadow], a"
                and dead_after(lines, k + 1, "azc")):
            lines[i] = "    sbc a ; carry capture as 0/$FF (flag_dead_peepholes)"
            lines[j] = "    ; (was ld a,0 / rl a)"
            nc += 1
            continue
        if dead_after(lines, i + 1, "zc"):
            lines[i] = l.replace("ld a, $00", "xor a ; flags dead (flag_dead_peepholes)", 1)
            nx += 1
    open(path, "w").write("\n".join(lines))
    print(f"flag_dead_peepholes: {nx} xor a, {nc} carry captures", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
