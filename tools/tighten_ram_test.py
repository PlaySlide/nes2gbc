#!/usr/bin/env python3
"""Tighten inline RAM tests of dynamic (indexed / (zp),Y / (zp,X)) accesses.

The emitters test the NES address high byte against $20 and mirror-fold it:
    ld a, h / cp $20 / jr nc, :+ / and $07 / or $C0 / ld h, a / ... / jr :++
    : / <generic bus call> / :
Testing against $08 instead lets the hit path drop the `and $07` (A < 8
already). Mirror addresses $0800-$1FFF then take the generic path, which still
maps them (nes_cpu_read*/nes_cpu_write handle $0000-$1FFF). Only sites whose
slow path is exactly one whitelisted bus helper (plus BC/AF save/restore or a
view-bank load) are touched. Runs last. NES2GBC_TIGHTEN_RAM_TEST=0 disables.
"""
import os
import re
import sys
from pathlib import Path

SAFE_CALLS = {"nes_cpu_read", "nes_cpu_read_hi", "nes_cpu_read_hi_view",
              "nes_cpu_read_hi32", "nes_cpu_write"}
SAFE_OTHER = re.compile(r"^(push|pop) (af|bc)$|^ld a, \$[0-9A-Fa-f]{2}$|^ld a, [bcde]$")


def code(l):
    return l.split(";", 1)[0].strip()


def main(path):
    if os.environ.get("NES2GBC_TIGHTEN_RAM_TEST", "1") == "0":
        print("tighten-ram-test: disabled")
        return
    p = Path(path)
    lines = p.read_text().splitlines(keepends=True)
    idx = [i for i, l in enumerate(lines) if code(l)]
    pos = {i: n for n, i in enumerate(idx)}
    n_ok = 0
    for n, i in enumerate(idx):
        if code(lines[i]) != "cp $20" or n < 1 or n + 3 >= len(idx):
            continue
        if code(lines[idx[n - 1]]) not in ("ld a, h", "ld h, a"):
            continue
        # comment lines between prev and cp are fine; a label is not
        if any(lines[k].lstrip().startswith(":") or (code(lines[k]).endswith(":")) for k in range(idx[n - 1] + 1, i)):
            continue
        if code(lines[idx[n + 1]]) != "jr nc, :+" or code(lines[idx[n + 2]]) != "and $07":
            continue
        # fast body up to "jr :++", no labels/jumps/calls inside
        m = n + 3
        ok = True
        while m < len(idx) and code(lines[idx[m]]) != "jr :++":
            c = code(lines[idx[m]])
            if c.endswith(":") or c.startswith(("jr", "jp", "call", "ret", "rst")) or c == ":":
                ok = False
                break
            m += 1
        if not ok or m + 1 >= len(idx) or code(lines[idx[m + 1]]) != ":":
            continue
        s = m + 2
        calls = 0
        while s < len(idx) and code(lines[idx[s]]) != ":":
            c = code(lines[idx[s]])
            if c.startswith("call "):
                if c[5:].strip() not in SAFE_CALLS:
                    ok = False
                    break
                calls += 1
            elif not SAFE_OTHER.match(c):
                ok = False
                break
            s += 1
        if not ok or calls != 1 or s >= len(idx):
            continue
        ind = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
        lines[i] = f"{ind}cp $08 ; tighten_ram_test: mirrors take the bus path\n"
        k = idx[n + 2]
        lines[k] = f"{ind}; (and $07 dropped: A < $08)\n"
        n_ok += 1
    p.write_text("".join(lines))
    print(f"tighten-ram-test: {n_ok} inline RAM tests narrowed to $0000-$07FF")


if __name__ == "__main__":
    main(sys.argv[1])
