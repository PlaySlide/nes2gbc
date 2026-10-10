#!/usr/bin/env python3
"""Collapse `jr cc, :+ / jp LABEL / :` into `jp !cc, LABEL`.

The branch-over-jump shape is left behind by later passes (repack direct
transfers, fallthrough layout, host flag branches). A conditional JP saves
2 bytes, and 2 M-cycles on the jump path (fall-through cost is unchanged).
The anonymous label is kept so other `:+`/`:-` references still resolve.
NES2GBC_SKIP_JP_COLLAPSE=0 disables.
"""
import os
import re
import sys

INV = {"z": "nz", "nz": "z", "c": "nc", "nc": "c"}
JR = re.compile(r"^(\s+)jr (n?z|n?c), :\+\s*(;.*)?$")
JP = re.compile(r"^\s+jp (nes_[A-Za-z0-9_.]+)\s*(;.*)?$")


def main() -> int:
    path = sys.argv[1]
    if os.environ.get("NES2GBC_SKIP_JP_COLLAPSE", "1") == "0":
        return 0
    lines = open(path).read().split("\n")
    out = []
    i = n = 0
    while i < len(lines):
        m = JR.match(lines[i])
        if m and i + 2 < len(lines):
            j = JP.match(lines[i + 1])
            if j and lines[i + 2].strip() == ":":
                out.append(f"{m.group(1)}jp {INV[m.group(2)]}, {j.group(1)} ; collapsed jr-over-jp")
                out.append(lines[i + 2])
                i += 3
                n += 1
                continue
        out.append(lines[i])
        i += 1
    open(path, "w").write("\n".join(out))
    print(f"skip_jp_collapse: {n} rewrites", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
