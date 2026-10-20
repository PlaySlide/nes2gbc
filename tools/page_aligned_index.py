#!/usr/bin/env python3
"""Drop the page-cross carry of indexed addresses on page-aligned bases.

    ld hl, $XX00 / add l / ld l, a / jr nc, :+ / inc h / :
adds an 8-bit index to a base whose low byte is 0: `add l` cannot change A
or carry, so the sequence is `ld hl, $XX00 / ld l, a`. The anonymous label
stays (other anonymous references keep their meaning). Flags after the
sequence were path-dependent before and are not consumed by the emitters.
NES2GBC_PAGE_ALIGNED_INDEX=0 disables.
"""
import os
import re
import sys
from pathlib import Path


def code(l):
    return l.split(";", 1)[0].strip()


def main(path):
    if os.environ.get("NES2GBC_PAGE_ALIGNED_INDEX", "1") == "0":
        print("page-aligned-index: disabled")
        return
    p = Path(path)
    lines = p.read_text().splitlines(keepends=True)
    idx = [i for i, l in enumerate(lines) if code(l)]
    n = 0
    for k in range(len(idx) - 5):
        if not re.fullmatch(r"ld hl, \$[0-9A-Fa-f]{2}00", code(lines[idx[k]])):
            continue
        seq = [code(lines[idx[k + d]]) for d in range(1, 6)]
        if seq != ["add l", "ld l, a", "jr nc, :+", "inc h", ":"]:
            continue
        # the flags left by `add l` must not be read before being rewritten
        f = k + 6
        safe = False
        need = {"z", "c"}
        while f < len(idx):
            c = code(lines[idx[f]])
            if c == ":" or re.match(r"^(ld|ldh) ", c) or re.match(r"^(push|pop) (bc|de|hl)$", c) \
                    or c.startswith("PROFILE_INC "):
                f += 1
                continue
            m = re.match(r"^(inc|dec) ([abcdehl]|\[hl\])$", c)
            if m:
                need.discard("z")  # Z/N/H rewritten, C kept
                f += 1
                continue
            if re.match(r"^(inc|dec) (bc|de|hl|sp)$", c):
                f += 1
                continue
            if re.match(r"^(add|sub|and|or|xor|cp) ", c) or c in ("pop af", "scf"):
                safe = True
            elif re.match(r"^nes_(?:m\d+_b[0-9A-Fa-f]{2}_)?[0-9A-Fa-f]{4}(?:_trace)?:$", c):
                safe = True
            elif re.match(r"^(jp|jr) [^,]+$", c) or c.startswith("call ") or c == "ret":
                safe = True
            else:
                mc = re.match(r"^(jp|jr|ret|call) (z|nz|c|nc)\b", c)
                if mc and (("z" if "z" in mc.group(2) else "c") not in need) \
                        and re.search(r", nes_(?:m\d+_b[0-9A-Fa-f]{2}_)?[0-9A-Fa-f]{4}(?:_trace)?$", c):
                    f += 1
                    continue
            break
        if not safe:
            continue
        ind = lines[idx[k + 1]][: len(lines[idx[k + 1]]) - len(lines[idx[k + 1]].lstrip())]
        for d in (1, 3, 4):
            lines[idx[k + d]] = f"{ind}; page-aligned base: no page cross (removed {code(lines[idx[k + d]])})\n"
        n += 1
    p.write_text("".join(lines))
    print(f"page-aligned-index: {n} carry checks removed")


if __name__ == "__main__":
    main(sys.argv[1])
