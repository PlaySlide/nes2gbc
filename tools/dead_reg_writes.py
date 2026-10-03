#!/usr/bin/env python3
"""Drop side-effect-free register writes whose results are never read (late pass).

Generalises dead_af_compute.py to every 8-bit register with the per-flag
model in tools/gbregs.py, e.g. a `ld d, a ; refresh DE cache nes_a` whose D is
rewritten before any read. A candidate writes only registers/flags (no memory
write, no HL post-increment, no stack); it is removed when every register and
flag it writes is dead. Liveness is a forward straight-line scan: labels are
transparent, `jp nes_XXXX` to a translated block head (SECTION entry, entered
by the dispatcher with no live registers) kills everything, a conditional
`jp cc, <block head>` continues on the fall-through path, a forward branch
to an anonymous label needs deadness on both paths, the trace-only
IF DEF(NES2GBC_PROFILE_TRACE) PC log is skipped (it reads nothing live, keeps
AF/BC and only kills D/E/H/L), and anything else ends the scan as live.
Iterates to a fixpoint. Code inside IF/ENDC is never touched.
"""
import bisect, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gbregs import code, effect, is_label, split  # noqa: E402

ANON_REF = re.compile(r"(?<![\w.]):(\++|-+)(?!\w)")


def candidate(c):
    op, a = split(c)
    if op is None:
        return None
    e = effect(c)
    if e is None:
        return None
    if op in ("ld", "ldh"):
        if len(a) != 2 or a[0].startswith("[") or "hli" in a[1] or "hld" in a[1] or "hl+" in a[1] or "hl-" in a[1]:
            return None
        return e[1]
    if op in ("push", "pop", "set", "res") or any(x.startswith("[") and op in ("inc", "dec") for x in a):
        return None
    if op in ("rlc", "rrc", "sla", "sra", "srl", "swap", "rl", "rr") and a and a[0].startswith("["):
        return None
    return e[1]


def main(path):
    p = Path(path)
    lines = p.read_text().splitlines(keepends=True)
    codes = [code(l) for l in lines]
    heads = set()
    for i in range(1, len(lines)):
        m = re.match(r"^(nes_[0-9A-F]{4}):$", codes[i])
        if m and lines[i - 1].startswith("SECTION"):
            heads.add(m.group(1))
    depth = [0] * len(lines); d = 0
    for i, c in enumerate(codes):
        if re.match(r"^IF\b", c):
            d += 1
        depth[i] = d
        if c.startswith("ENDC"):
            d -= 1

    anon = [i for i, c in enumerate(codes) if c == ":"]
    target = {}
    for i, c in enumerate(codes):
        if c == ":" or ":" not in c:
            continue
        for m in ANON_REF.finditer(c):
            s_ = m.group(1); k = bisect.bisect_right(anon, i)
            t = k + len(s_) - 1 if s_[0] == "+" else k - len(s_)
            if 0 <= t < len(anon):
                target[i] = anon[t]

    def dead_after(i, regs, budget=4):
        return dead_from(i + 1, set(regs), budget)

    def dead_from(k, need, budget):
        while k < len(lines) and need:
            c = codes[k]
            if not c or is_label(c) or c.startswith("PROFILE_INC"):
                k += 1; continue
            if c == "IF DEF(NES2GBC_PROFILE_TRACE)":
                while k < len(lines) and codes[k] != "ENDC":
                    k += 1
                k += 1; continue
            m = re.match(r"^jp (?:(?:n?[zc]), )?(nes_[0-9A-F]{4})$", c)
            if m and m.group(1) in heads:
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
                if not dead_from(target[k] + 1, set(need), budget - 1):
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

    total = 0
    while True:
        n = 0
        for i in range(len(lines) - 1, -1, -1):
            c = codes[i]
            if not c or depth[i]:
                continue
            regs = candidate(c)
            if regs and dead_after(i, regs):
                ind = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
                lines[i] = f"{ind}; dead register write removed: {c}\n"
                codes[i] = ""
                n += 1
        total += n
        if not n:
            break
    p.write_text("".join(lines))
    print(f"dead-reg-writes: {total} dead register writes removed")


if __name__ == "__main__":
    main(sys.argv[1])
