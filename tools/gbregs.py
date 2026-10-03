"""Register read/write model for generated LR35902 asm lines (late passes).

effect(code) -> (reads, writes) over {'a','b','c','d','e','h','l'} plus the
individual flags 'zf','nf','hf','cf' (F = all four), or None for control
transfers, directives and anything not understood.
"""
import bisect
import re

R8 = {"a", "b", "c", "d", "e", "h", "l"}
F = {"zf", "nf", "hf", "cf"}
ZNH = {"zf", "nf", "hf"}
PAIRS = {"bc": {"b", "c"}, "de": {"d", "e"}, "hl": {"h", "l"}, "af": {"a"} | F}
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
            return r, set(ZNH)
        return r, r | ZNH
    if op in ALU:
        if len(a) == 2 and a[0] == "a":
            a = a[1:]
        if len(a) == 2 and a[0] == "hl":
            if op != "add" or a[1] not in ("bc", "de", "hl"):
                return None
            return {"h", "l"} | PAIRS[a[1]], {"h", "l", "nf", "hf", "cf"}
        if len(a) != 1:
            return None
        if a[0] == "sp":
            return None
        s = a[0]
        if op in ("xor", "sub") and s == "a":
            return set(), {"a"} | F
        r, _ = operand(s)
        r = r | {"a"}
        if op in ("adc", "sbc"):
            r.add("cf")
        return r, (set(F) if op == "cp" else {"a"} | F)
    if op in ("rlca", "rrca"):
        return {"a"}, {"a"} | F
    if op in ("rla", "rra"):
        return {"a", "cf"}, {"a"} | F
    if op == "cpl":
        return {"a"}, {"a", "nf", "hf"}
    if op == "daa":
        return {"a"} | F, {"a"} | F
    if op == "scf":
        return set(), {"nf", "hf", "cf"}
    if op == "ccf":
        return {"cf"}, {"nf", "hf", "cf"}
    if op in CB_ROT and len(a) == 1:
        r, mem = operand(a[0])
        rd = set(r) | ({"cf"} if op in ("rl", "rr") else set())
        return rd, (set(F) if mem else r | F)
    if op in ("bit", "set", "res") and len(a) == 2:
        r, mem = operand(a[1])
        if op == "bit":
            return set(r), set(ZNH)
        return set(r), (set() if mem else set(r))
    if op == "push" and len(a) == 1 and a[0] in PAIRS:
        return set(PAIRS[a[0]]), set()
    if op == "pop" and len(a) == 1 and a[0] in PAIRS:
        return set(), set(PAIRS[a[0]])
    return None


ANON_REF = re.compile(r"(?<![\w.]):(\++|-+)(?!\w)")


def if_depth(codes):
    depth = [0] * len(codes); d = 0
    for i, c in enumerate(codes):
        if re.match(r"^IF\b", c):
            d += 1
        depth[i] = d
        if c.startswith("ENDC"):
            d -= 1
    return depth


class Liveness:
    """Forward straight-line deadness queries over generated asm lines.

    Labels are transparent; `jp nes_XXXX` to a translated block head (SECTION
    entry, entered by the dispatcher with no live registers) kills
    everything; a conditional `jp cc, <block head>` continues on the
    fall-through path; a forward branch to an anonymous label needs deadness
    on both paths; the trace-only IF DEF(NES2GBC_PROFILE_TRACE) PC log is
    skipped (reads nothing live, keeps AF/BC, only kills D/E/H/L); anything
    else ends the scan as live.
    """

    def __init__(self, lines, codes):
        self.lines, self.codes = lines, codes
        self.heads = set()
        for i in range(1, len(lines)):
            m = re.match(r"^(nes_[0-9A-F]{4}):$", codes[i])
            if m and lines[i - 1].startswith("SECTION"):
                self.heads.add(m.group(1))
        anon = [i for i, c in enumerate(codes) if c == ":"]
        self.target = {}
        for i, c in enumerate(codes):
            if c == ":" or ":" not in c:
                continue
            for m in ANON_REF.finditer(c):
                s_ = m.group(1); k = bisect.bisect_right(anon, i)
                t = k + len(s_) - 1 if s_[0] == "+" else k - len(s_)
                if 0 <= t < len(anon):
                    self.target[i] = anon[t]

    def dead_after(self, i, regs, budget=4):
        return self.dead_from(i + 1, set(regs), budget)

    def dead_from(self, k, need, budget):
        lines, codes, target = self.lines, self.codes, self.target
        while k < len(lines) and need:
            c = codes[k]
            if not c or is_label(c) or c.startswith("PROFILE_INC"):
                k += 1; continue
            if c == "IF DEF(NES2GBC_PROFILE_TRACE)":
                while k < len(lines) and codes[k] != "ENDC":
                    k += 1
                k += 1; continue
            m = re.match(r"^jp (?:(?:n?[zc]), )?(nes_[0-9A-F]{4})$", c)
            if m and m.group(1) in self.heads:
                if "," not in c:
                    return True
                if any(f in need for f in ("zf", "cf")):
                    return False  # the condition reads a flag we need dead... conservatively live
                k += 1; continue
            mj = re.match(r"^(jr|jp) (?:(n?[zc]), )?:\+{1,2}$", c)
            if mj and k in target and target[k] > k and budget > 0:
                flag = {"z": "zf", "nz": "zf", "c": "cf", "nc": "cf"}.get(mj.group(2))
                if flag and flag in need:
                    return False
                if not self.dead_from(target[k] + 1, set(need), budget - 1):
                    return False
                if not mj.group(2):
                    return True
                k += 1; continue
            e = effect(c)
            if e is None:
                return False
            r, w = e
            if r & need:
                return False
            need -= w; k += 1
        return not need

