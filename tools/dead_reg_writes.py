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
import re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gbregs import Liveness, code, effect, if_depth, split  # noqa: E402


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
    depth = if_depth(codes)
    dead_after = Liveness(lines, codes).dead_after

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
