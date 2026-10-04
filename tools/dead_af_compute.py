#!/usr/bin/env python3
"""Drop side-effect-free A/F computations whose results are never read (late pass).

Typical leftover once a flag shadow store was elided as dead:

    sub e                 ; compare (its flags feed the next branch? no:)
    sbc a                 ; cheap carry materialize ...
    inc a                 ; ... whose nes_c_shadow store was removed
:
    ldh a, [nes_z_shadow] ; A overwritten, flags overwritten below
    and a

Candidates only write A and/or F (no memory write, no HL/SP side effect). A
candidate is removed when, scanning forward along straight-line code, every
register it writes is overwritten before any read. Liveness is a backward
property, so labels on the way are transparent; any control transfer,
directive or unrecognised line ends the scan as "live". Iterates to a fixpoint.
"""
import re, sys
from pathlib import Path

R8 = {"a", "b", "c", "d", "e", "h", "l"}
ALU = {"add", "adc", "sub", "sbc", "and", "or", "xor", "cp"}
CB_FULLF = {"rlc", "rrc", "sla", "sra", "srl", "swap"}  # write all of F, read no flag
CB_READC = {"rl", "rr"}


def code(l):
    return l.split(";", 1)[0].strip()


def is_label(c):
    return c == ":" or re.match(r"^[.\w]+:{1,2}$", c) is not None


def split(c):
    m = re.match(r"^(\w+)\s*(.*)$", c)
    if not m:
        return None, []
    ops = [o.strip().lower() for o in m.group(2).split(",")] if m.group(2) else []
    return m.group(1).lower(), ops


def effect(c):
    """Return (reads, writes) as sets over {'A','F'} or None if unknown/control."""
    op, a = split(c)
    if op is None:
        return None
    if op == "nop":
        return set(), set()
    if op in ("ld", "ldh") and len(a) == 2:
        d, s = a
        if d in ("hl",) and s.startswith("sp"):
            return None  # ld hl, sp+e writes flags
        r = {"A"} if s == "a" else set()
        w = {"A"} if d == "a" else set()
        if d == "a" and s == "a":
            r = {"A"}
        return r, w
    if op in ("inc", "dec") and len(a) == 1:
        if a[0] in ("bc", "de", "hl", "sp"):
            return set(), set()
        # 8-bit inc/dec: writes Z/N/H, keeps C -> treat F as read (partial)
        return ({"A", "F"} if a[0] == "a" else {"F"}), ({"A", "F"} if a[0] == "a" else {"F"})
    if op in ALU:
        if len(a) == 2 and a[0] == "a":
            a = a[1:]
        if len(a) != 1:
            return None
        s = a[0]
        if op in ("xor", "sub") and s == "a":
            return set(), {"A", "F"}
        r = {"A"}
        if op in ("adc", "sbc"):
            r.add("F")
        w = {"F"} if op == "cp" else {"A", "F"}
        return r, w
    if op in ("rlca", "rrca"):
        return {"A"}, {"A", "F"}
    if op in ("rla", "rra"):
        return {"A", "F"}, {"A", "F"}
    if op in ("cpl",):
        return {"A", "F"}, {"A", "F"}
    if op in ("scf", "ccf"):
        return {"F"}, {"F"}
    if op in CB_FULLF and len(a) == 1:
        return ({"A"} if a[0] == "a" else set()), ({"A", "F"} if a[0] == "a" else {"F"})
    if op in CB_READC and len(a) == 1:
        return ({"A", "F"} if a[0] == "a" else {"F"}), ({"A", "F"} if a[0] == "a" else {"F"})
    if op in ("bit", "set", "res") and len(a) == 2:
        rd = {"A"} if a[1] == "a" else set()
        if op == "bit":
            return rd | {"F"}, {"F"}
        return rd, ({"A"} if a[1] == "a" else set())
    if op == "push" and a == ["af"]:
        return {"A", "F"}, set()
    if op == "push" or op == "pop" and a != ["af"]:
        return set(), set()
    return None


def candidate(c):
    """Return the set of registers written if c is a removable pure A/F op."""
    op, a = split(c)
    if op is None:
        return None
    if op in ("ld", "ldh") and len(a) == 2 and a[0] == "a":
        s = a[1].replace(" ", "")
        if s in ("a",) or s.startswith("[hl+") or s.startswith("[hl-") or s.startswith("[hli") or s.startswith("[hld"):
            return None
        return {"A"}
    if op in ALU and op != "cp":
        e = effect(c)
        return e[1] if e else None
    if op in ("inc", "dec") and a == ["a"]:
        return {"A", "F"}
    if op in ("rlca", "rrca", "rla", "rra", "cpl", "scf", "ccf"):
        return effect(c)[1]
    if op in CB_FULLF | CB_READC and a == ["a"]:
        return {"A", "F"}
    if op == "cp":
        return {"F"}
    return None


def dead_after(lines, i, regs):
    need = set(regs)
    j = i + 1
    while j < len(lines) and need:
        c = code(lines[j])
        if not c or is_label(c):
            j += 1
            continue
        e = effect(c)
        if e is None:
            return False
        r, w = e
        if r & need:
            return False
        need -= w
        j += 1
    return not need


def main(path):
    p = Path(path)
    lines = p.read_text().splitlines(keepends=True)
    total = 0
    while True:
        n = 0
        for i in range(len(lines) - 1, -1, -1):
            c = code(lines[i])
            if not c:
                continue
            regs = candidate(c)
            if regs and dead_after(lines, i, regs):
                ind = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
                lines[i] = f"{ind}; dead A/F computation removed: {c}\n"
                n += 1
        total += n
        if not n:
            break
    p.write_text("".join(lines))
    print(f"dead-af-compute: {total} dead A/F instructions removed")


if __name__ == "__main__":
    main(sys.argv[1])
