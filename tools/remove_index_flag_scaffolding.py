#!/usr/bin/env python3
"""Remove host-flag scaffolding left by exact indexed-address simplifications.

`index_math_generated.py` intentionally leaves an `and a` after two address-only
rewrites:

  * page-aligned absolute base ($xx00 + X/Y)
  * zero-page base $00 + X/Y

Those operations exist only to form an effective address; 6502 indexed address
calculation does not modify architectural flags.  At these exact marker sites the
carry branch/high-byte fixup has already been removed, so no remaining generated
instruction consumes the GB flags produced by `and a`.

Loads subsequently establish their own 6502 Z/N through canonical result
publication/branch tests, stores restore AF before publishing the value, and
non-RAM dynamic paths perform their own compares before branching.  Therefore
these marker-tagged `and a` instructions are pure host bookkeeping and can be
removed without changing NES-visible state.
"""

from __future__ import annotations

import argparse
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("asm", type=Path)
    args = p.parse_args()

    lines = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    page = 0
    zp0 = 0

    for i, line in enumerate(lines):
        if "and a ; page-aligned absolute index, no carry" in line:
            ind = line[: len(line) - len(line.lstrip())]
            lines[i] = f"{ind}; dead host-flag scaffold removed: page-aligned index\n"
            page += 1
        elif "and a ; zero-page base $00" in line:
            ind = line[: len(line) - len(line.lstrip())]
            lines[i] = f"{ind}; dead host-flag scaffold removed: zero-page $00 index\n"
            zp0 += 1

    args.asm.write_text("".join(lines), encoding="utf-8")
    print(
        f"index-flags: removed {page} page-aligned + {zp0} zero-page-$00 host flag ops"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
