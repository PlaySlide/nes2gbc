#!/usr/bin/env python3
"""Move the taken half of translated NMI polls out of line.

Generated safe-points read
    ldh a, [nes_host_vblank_pending] / and a / jr z, :+
    <poll call + NMI entry jump> / :
so the common no-VBlank case pays a taken JR (12 cycles). This pass inverts
the test into `jr nz, .npoll_slow_N` (8 cycles not taken) and moves the poll
body to a stub after the SECTION's final unconditional transfer; the stub
jumps back to the original continuation. Same A/flags on every path.

Only applied when the stub provably stays in JR range (every instruction
charged 3 bytes, PROFILE_INC charged 48, any other directive aborts) and the
section ends in an unconditional jp/jr/ret. Runs right before
widen_generated_jumps.py. NES2GBC_OUTLINE_NMI_POLLS=0 disables.
"""
import os
import re
import sys
from pathlib import Path

MNEM = {"adc", "add", "and", "bit", "call", "ccf", "cp", "cpl", "daa", "dec", "di", "ei",
        "halt", "inc", "jp", "jr", "ld", "ldh", "nop", "or", "pop", "push", "res", "ret",
        "reti", "rl", "rla", "rlc", "rlca", "rr", "rra", "rrc", "rrca", "rst", "sbc", "scf",
        "set", "sla", "sra", "srl", "stop", "sub", "swap", "xor"}
LABEL_RE = re.compile(r"^[A-Za-z_.$@][A-Za-z0-9_.$@]*:{1,2}$")


def code(l):
    return l.split(";", 1)[0].strip()


def nbytes(l):
    c = code(l)
    if not c or c == ":" or LABEL_RE.fullmatch(c):
        return 0
    if c.startswith(("IF DEF(", "IF !DEF(", "ELSE", "ENDC")):
        return 0
    m = c.split(None, 1)[0]
    if m.lower() in MNEM:
        return 3
    if m == "PROFILE_INC":
        return 48
    return None


def main(path):
    if os.environ.get("NES2GBC_OUTLINE_NMI_POLLS", "1") == "0":
        print("outline-nmi-polls: disabled")
        return
    p = Path(path)
    lines = p.read_text().splitlines(keepends=True)
    # section boundaries
    sec_end = [0] * len(lines)
    end = len(lines)
    for i in range(len(lines) - 1, -1, -1):
        if code(lines[i]).startswith("SECTION "):
            sec_end[i] = end
            end = i
        else:
            sec_end[i] = end
    depth = [0] * len(lines)
    d = 0
    for t, l in enumerate(lines):
        c = code(l)
        if c.startswith("IF "):
            d += 1
        elif c == "ENDC":
            d -= 1
        depth[t] = d
    inserts = {}  # line index -> stub text to insert before it
    nstub = 0
    i = 0
    while i < len(lines) - 3:
        if code(lines[i]) != "ldh a, [nes_host_vblank_pending]" or code(lines[i + 1]) != "and a" \
                or code(lines[i + 2]) != "jr z, :+" or depth[i] != 0:
            i += 1
            continue
        j = i + 3
        body = []
        ok = True
        while j < len(lines) and code(lines[j]) != ":":
            c = code(lines[j])
            if c.endswith(":") or ":+" in c or ":-" in c or c.startswith("SECTION") or c.startswith(("IF", "ENDC", "ELSE")):
                ok = False
                break
            if c:
                body.append(lines[j])
            j += 1
        if not ok or j >= len(lines) or not body:
            i += 1
            continue
        # stub goes after the first unconditional transfer ahead (same
        # SECTION, outside IF blocks) that is provably within JR range
        e = None
        dist = 0
        for t in range(i + 3, sec_end[i]):
            dist += 3 * sum(len(x) for x in inserts.get(t, []))
            c = code(lines[t])
            if LABEL_RE.fullmatch(c) and not c.startswith("."):
                break  # new global label scope: local stub labels would not resolve
            if t >= j:
                if depth[t] == 0 and (re.match(r"^(jp|jr) [^,]+$", c) or c in ("ret", "reti")):
                    if dist + 3 + 3 * sum(len(x) for x in inserts.get(t + 1, [])) <= 127:
                        e = t + 1
                    break
            b = nbytes(lines[t])
            if b is None:
                break
            dist += b
            if dist > 127:
                break
        if e is None:
            i += 1
            continue
        ind = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
        lab = f".npoll_slow_{nstub}"
        back = f".npoll_back_{nstub}"
        nstub += 1
        lines[i + 2] = f"{ind}jr nz, {lab} ; outline_nmi_polls\n"
        for t in range(i + 3, j):
            if code(lines[t]):
                lines[t] = f"{ind}; (moved to {lab})\n"
        lines[j] = f"{back}:\n"
        stub = [f"{lab}:"] + [b.rstrip("\n") for b in body] + [f"    jp {back}"]
        inserts.setdefault(e, []).append(stub)
        i = j + 1
    out = []
    for idx, l in enumerate(lines):
        if idx in inserts:
            for s in inserts[idx]:
                out.extend(x + "\n" for x in s)
        out.append(l)
    if len(lines) in inserts:
        for s in inserts[len(lines)]:
            out.extend(x + "\n" for x in s)
    p.write_text("".join(out))
    print(f"outline-nmi-polls: {nstub} poll bodies moved out of line")


if __name__ == "__main__":
    main(sys.argv[1])
