#!/usr/bin/env python3
"""Reuse HL between nearby indexed RAM accesses with the same index register.

Translated abs,X / abs,Y RAM accesses compute HL = base + index as

    [push af] / ld hl, B / ld a, r / add l / ld l, a / jr nc, :+ / inc h / : /
    [pop af] / ld [hl], a        (or: ... / : / ld a, [hl])

When HL still holds B0 + r from the previous such access (same r, nothing in
between touched H, L or r, no labels/transfers) and |B - B0| <= 6, the
address computation becomes inc hl / dec hl (exact 16-bit arithmetic, no
flag or A effects) and the push/pop pair that protected A is dropped.
"""
import re, sys
from pathlib import Path


def code(l):
    return l.split(";", 1)[0].strip()


CORE = re.compile(r"ld hl, \$([0-9A-Fa-f]{4})$")
WRITES_HL = re.compile(r"^(ld|ldh|pop|inc|dec|add|adc|sub|sbc|and|or|xor|swap|rl|rr|rlc|rrc|sla|sra|srl|res|set)\b")


def touches(c, regs):
    """True if instruction c may modify any register in regs (subset of 'hlbcde')."""
    op, _, rest = c.partition(" ")
    args = [x.strip() for x in rest.split(",")] if rest else []
    if op in ("ld", "ldh"):
        d = args[0] if args else ""
        if d in regs:
            return True
        if d in ("hl", "bc", "de", "sp") and set(d) & set(regs):
            return True
        if d in ("[hli]", "[hld]", "[hl+]", "[hl-]") or (len(args) > 1 and args[1] in ("[hli]", "[hld]", "[hl+]", "[hl-]")):
            return bool(set("hl") & set(regs))
        return False
    if op in ("inc", "dec"):
        r = args[0]
        return r in regs or (r in ("hl", "bc", "de") and bool(set(r) & set(regs)))
    if op == "pop":
        return bool(set(args[0]) & set(regs)) if args[0] != "af" else False
    if op == "add" and args and args[0] == "hl":
        return bool(set("hl") & set(regs))
    if op in ("swap", "rl", "rr", "rlc", "rrc", "sla", "sra", "srl", "res", "set"):
        return args[-1] in regs
    if op in ("and", "or", "xor", "adc", "sub", "sbc", "cp", "add", "push", "cpl", "rla", "rra",
              "rlca", "rrca", "scf", "ccf", "nop", "daa", "bit", "PROFILE_INC"):
        return False
    return True  # unknown / control transfer


def main(path):
    p = Path(path)
    text = p.read_text()
    if re.search(r"\b(jr|jp)\b[^;\n]*:-", text):
        print("chain-indexed-hl: backward anonymous labels present; skipped")
        return
    L = text.splitlines(keepends=True)
    idx = [i for i, l in enumerate(L) if code(l)]
    C = [code(L[i]) for i in idx]
    n = 0
    state = None  # (base, reg)
    k = 0
    rm = set()
    repl = {}
    while k < len(C):
        c = C[k]
        m = CORE.fullmatch(c)
        if m and k + 6 < len(C) and re.fullmatch(r"ld a, [bcde]", C[k + 1]) and C[k + 2] == "add l" \
                and C[k + 3] == "ld l, a" and C[k + 4] == "jr nc, :+" and C[k + 5] == "inc h" and C[k + 6] == ":":
            base = int(m.group(1), 16)
            r = C[k + 1][-1]
            wrapped = k > 0 and C[k - 1] == "push af" and k + 7 < len(C) and C[k + 7] == "pop af"
            nxt = C[k + 8] if wrapped else C[k + 7]
            ok_next = nxt == "ld [hl], a" if wrapped else nxt == "ld a, [hl]"
            if state and state[1] == r and ok_next and abs(base - state[0]) <= 6 and (base >> 8) >= 0xC0:
                d = base - state[0]
                ins = ["inc hl"] * d if d > 0 else ["dec hl"] * (-d)
                lo, hi = (k - 1, k + 7) if wrapped else (k, k + 6)
                for q in range(lo, hi + 1):
                    rm.add(idx[q])
                repl[idx[k]] = ins
                n += 1
            state = (base, r) if (base >> 8) >= 0xC0 else None
            k += 8 if wrapped else 7
            continue
        if c.endswith(":") or c.startswith(("IF", "ELSE", "ENDC", "SECTION", "ELIF", "REPT", "ENDR")):
            state = None
        elif state and touches(c, "hl" + state[1]):
            state = None
        k += 1
    out = []
    for i, l in enumerate(L):
        if i in repl:
            out += ["    " + x + " ; chained indexed address\n" for x in repl[i]]
        elif i in rm:
            continue
        else:
            out.append(l)
    p.write_text("".join(out))
    print(f"chain-indexed-hl: {n} address computations chained")


if __name__ == "__main__":
    main(sys.argv[1])
