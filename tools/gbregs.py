"""Register read/write model for generated LR35902 asm lines (late passes).

effect(code) -> (reads, writes) over {'a','f','b','c','d','e','h','l'} or
None for control transfers, directives and anything not understood.
"""
import re

R8 = {"a", "b", "c", "d", "e", "h", "l"}
PAIRS = {"bc": {"b", "c"}, "de": {"d", "e"}, "hl": {"h", "l"}, "af": {"a", "f"}}
ALU = {"add", "adc", "sub", "sbc", "and", "or", "xor", "cp"}
CB_ROT = {"rlc", "rrc", "sla", "sra", "srl", "swap", "rl", "rr"}


def code(l):
    return l.split(";", 1)[0].strip()


def is_label(c):
    return c == ":" or re.match(r"^[.\w]+:{1,2}$", c) is not None


def split(c):
    m = re.match(r"^(\w+)\s*(.*)$", c)
    if not m:
        return None, []
    ops = [o.strip().lower().replace(" ", "") for o in m.group(2).split(",")] if m.group(2) else []
    return m.group(1).lower(), ops


def operand(o):
    """(reads, is_mem) for an 8-bit source/dest operand."""
    if o in R8:
        return {o}, False
    if o.startswith("["):
        inner = o[1:-1]
        if inner in ("hl", "hli", "hld", "hl+", "hl-"):
            return {"h", "l"}, True
        if inner in ("de",):
            return {"d", "e"}, True
        if inner in ("bc",):
            return {"b", "c"}, True
        if inner == "c":
            return {"c"}, True
        return set(), True
    return set(), False  # immediate / symbol


def hl_post(o):
    return o.startswith("[") and o[1:-1] in ("hli", "hld", "hl+", "hl-")


def effect(c):
    op, a = split(c)
    if op is None:
        return None
    if op in ("nop", "di", "ei"):
        return set(), set()
    if op in ("ld", "ldh") and len(a) == 2:
        d, s = a
        if d in PAIRS and d != "af":
            if s.startswith("sp"):
                return None
            return set(), set(PAIRS[d])
        if d == "sp" or s == "sp" or s.startswith("sp"):
            return None
        sr, _ = operand(s)
        dr, dmem = operand(d)
        reads = set(sr)
        writes = set()
        if dmem:
            reads |= dr
        else:
            writes |= dr
        if hl_post(s) or hl_post(d):
            reads |= {"h", "l"}; writes |= {"h", "l"}
        return reads, writes
    if op in ("inc", "dec") and len(a) == 1:
        o = a[0]
        if o in PAIRS:
            return set(PAIRS[o]), set(PAIRS[o])
        if o == "sp":
            return None
        r, mem = operand(o)
        if mem:
            return r | {"f"}, {"f"}
        return r | {"f"}, r | {"f"}
    if op in ALU:
        if len(a) == 2 and a[0] == "a":
            a = a[1:]
        if len(a) == 2 and a[0] == "hl":
            return {"h", "l", "f"} | PAIRS.get(a[1], set()), {"h", "l", "f"}
        if len(a) != 1:
            return None
        if a[0] == "sp":
            return None
        s = a[0]
        if op in ("xor", "sub") and s == "a":
            return set(), {"a", "f"}
        r, _ = operand(s)
        r = r | {"a"}
        if op in ("adc", "sbc"):
            r.add("f")
        return r, ({"f"} if op == "cp" else {"a", "f"})
    if op in ("rlca", "rrca"):
        return {"a"}, {"a", "f"}
    if op in ("rla", "rra", "cpl", "daa"):
        return {"a", "f"}, {"a", "f"}
    if op in ("scf", "ccf"):
        return {"f"}, {"f"}
    if op in CB_ROT and len(a) == 1:
        r, mem = operand(a[0])
        rd = set(r) | ({"f"} if op in ("rl", "rr") else set())
        return rd, ({"f"} if mem else r | {"f"})
    if op in ("bit", "set", "res") and len(a) == 2:
        r, mem = operand(a[1])
        if op == "bit":
            return r | {"f"}, {"f"}
        return set(r), (set() if mem else set(r))
    if op == "push" and len(a) == 1 and a[0] in PAIRS:
        return set(PAIRS[a[0]]), set()
    if op == "pop" and len(a) == 1 and a[0] in PAIRS:
        return set(), set(PAIRS[a[0]])
    return None
