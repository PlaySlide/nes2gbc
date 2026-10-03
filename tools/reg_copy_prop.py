#!/usr/bin/env python3
"""Straight-line register/memory value numbering for generated code (late pass).

Removes copies that move a value into a register that already holds it, and
turns reloads of a tracked memory cell into register moves:

    ld d, a          ; refresh DE cache
    ld a, d          -> removed (A already equals D)
    ld d, a          -> removed
    ...
    ld a, c
    ldh [nes_y], a
    ld a, [de]
    ldh a, [nes_y]   -> ld a, c  (3 M -> 1 M; loads never touch flags)

Tracked cells: 6502-state HRAM shadows (only ever written by direct ldh
stores; no ISR writes them) and NES internal RAM $C000-$C7FF by literal
address (any store through a register pointer forgets all of NES RAM).
Knowledge is dropped at every referenced label, call, directive, and any line
the gbregs model does not understand; conditional branches keep it on the
fall-through path. Nothing inside IF/ENDC is rewritten.
"""
import bisect, itertools, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gbregs import code, effect, is_label, split  # noqa: E402

STATE = {"nes_a", "nes_x", "nes_y", "nes_z_shadow", "nes_n_shadow", "nes_c_shadow", "nes_sp", "nes_p"}
R8 = ("a", "b", "c", "d", "e", "h", "l")
ANON_REF = re.compile(r"(?<![\w.]):(\++|-+)(?!\w)")
IMM = re.compile(r"^(\$[0-9A-Fa-f]+|[0-9]+|%[01]+)$")


def cell(x):
    x = x.strip()
    if x in STATE:
        return x
    m = re.fullmatch(r"\$([0-9A-Fa-f]{4})", x)
    if m and 0xC000 <= int(m.group(1), 16) <= 0xC7FF:
        return "$%04X" % int(m.group(1), 16)
    return None


def immval(s):
    s = s.strip()
    if not IMM.match(s):
        return None
    if s.startswith("$"):
        return int(s[1:], 16) & 0xFF
    if s.startswith("%"):
        return int(s[1:], 2) & 0xFF
    return int(s) & 0xFF


def main(path):
    p = Path(path)
    lines = p.read_text().splitlines(keepends=True)
    codes = [code(l) for l in lines]
    anon = [i for i, c in enumerate(codes) if c == ":"]
    refd = set()
    for i, c in enumerate(codes):
        if c == ":" or ":" not in c:
            continue
        for m in ANON_REF.finditer(c):
            s = m.group(1); k = bisect.bisect_right(anon, i)
            t = k + len(s) - 1 if s[0] == "+" else k - len(s)
            if 0 <= t < len(anon):
                refd.add(anon[t])
            else:
                print("reg-copy-prop: unresolved anonymous ref, skipped"); return
    fresh = itertools.count()
    reg = {}; mem = {}

    def reset():
        reg.clear(); mem.clear()

    def new():
        return ("v", next(fresh))

    def forget_ram():
        for k in [k for k in mem if k.startswith("$")]:
            del mem[k]

    removed = subst = 0
    depth = 0
    for i, c in enumerate(codes):
        if not c:
            continue
        if re.match(r"^IF\b", c):
            depth += 1; reset(); continue
        if c.startswith("ENDC") or c.startswith("ELSE"):
            if c.startswith("ENDC"):
                depth -= 1
            reset(); continue
        if depth:
            reset(); continue
        if c.startswith("PROFILE_INC"):
            continue  # preserves AF/HL, profile builds only
        if is_label(c):
            if c == ":" and i not in refd:
                continue
            reset(); continue
        op, a = split(c)
        ind = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
        if op == "ld" and len(a) == 2 and a[0] in R8 and a[1] in R8:
            d, s = a
            if s not in reg:
                reg[s] = new()
            if reg.get(d) == reg[s]:
                lines[i] = f"{ind}; copy of an equal value removed: {c}\n"; removed += 1
                continue
            reg[d] = reg[s]
            continue
        if op == "ld" and len(a) == 2 and a[0] in R8 and immval(a[1]) is not None:
            v = ("imm", immval(a[1]))
            if reg.get(a[0]) == v:
                lines[i] = f"{ind}; load of an equal immediate removed: {c}\n"; removed += 1
                continue
            reg[a[0]] = v
            continue
        if op in ("ld", "ldh") and len(a) == 2 and a[0] == "a" and a[1].startswith("[") and cell(a[1][1:-1]):
            x = cell(a[1][1:-1])
            if x in mem:
                v = mem[x]
                if reg.get("a") == v:
                    lines[i] = f"{ind}; reload of a value already in A removed: {c}\n"; removed += 1
                    continue
                r = next((r for r in R8 if r != "a" and reg.get(r) == v), None)
                if r:
                    lines[i] = f"{ind}ld a, {r} ; {x} value already in {r.upper()} (was {c})\n"; subst += 1
                    reg["a"] = v
                    continue
                reg["a"] = v
                continue
            reg["a"] = mem[x] = new()
            continue
        if op in ("ld", "ldh") and len(a) == 2 and a[1] == "a" and a[0].startswith("[") and cell(a[0][1:-1]):
            x = cell(a[0][1:-1])
            if "a" not in reg:
                reg["a"] = new()
            mem[x] = reg["a"]
            continue
        if op in ("ld", "ldh") and len(a) == 2 and a[0].startswith("["):
            inner = a[0][1:-1]
            if inner == "c" and op == "ldh":
                reset()
            elif inner in ("hl", "hli", "hld", "hl+", "hl-", "de", "bc"):
                forget_ram()
            elif cell(inner) is None and not inner.startswith(("nes_", "r", "$FF", "$ff")):
                forget_ram()
            elif inner.startswith(("$C", "$c", "$D", "$d")):
                forget_ram()
        if op in ("jr", "jp"):
            if len(a) == 2:
                continue  # conditional: fall-through keeps all knowledge
            reset(); continue
        e = effect(c)
        if e is None:
            reset(); continue
        if op in ("inc", "dec", "rlc", "rrc", "rl", "rr", "sla", "sra", "srl", "swap", "set", "res") and any(x.startswith("[") for x in a):
            forget_ram()
        if op == "ld" and len(a) == 2 and a[0] in ("bc", "de", "hl") and immval(a[1]) is not None:
            v = int(a[1][1:], 16) if a[1].startswith("$") else None
            hi, lo = a[0][0], a[0][1]
            if v is not None:
                reg[hi] = ("imm", (v >> 8) & 0xFF); reg[lo] = ("imm", v & 0xFF)
                continue
        for r in e[1]:
            if r in R8:
                reg[r] = new()
    p.write_text("".join(lines))
    print(f"reg-copy-prop: {removed} redundant copies removed, {subst} reloads turned into register moves")


if __name__ == "__main__":
    main(sys.argv[1])
