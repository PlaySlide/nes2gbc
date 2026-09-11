#!/usr/bin/env python3
"""Widen generated NES-label JRs after size-expanding peepholes.

The Rust emitter decides whether a same-section target fits JR before the
post-generation peephole pass runs.  Compare/stack/PPU inlining can enlarge the
code between a branch and its target, invalidating that earlier range decision.
Convert direct generated-code JRs to range-independent JPs as the final pass.

For conditional branches this is still a win over the old two-instruction
`jr !cond, :+` / `jr target` shape after branch fusion: a single conditional JP
has no range limit and avoids the extra taken jump.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path


STATIC_JR_RE = re.compile(
    r"^(?P<indent>\s*)jr (?:(?P<cond>z|nz|c|nc), )?(?P<target>nes_[0-9A-Fa-f]{4})(?P<tail>\s*(?:;.*)?)$"
)


def widen(lines: list[str]) -> tuple[list[str], int]:
    out = list(lines)
    changed = 0
    for i, line in enumerate(out):
        raw = line.rstrip("\n")
        match = STATIC_JR_RE.fullmatch(raw)
        if not match:
            continue
        cond = match.group("cond")
        target = match.group("target")
        prefix = "jp " if cond is None else f"jp {cond}, "
        out[i] = (
            f"{match.group('indent')}{prefix}{target}{match.group('tail')}\n"
        )
        changed += 1
    return out, changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("asm", type=Path)
    args = parser.parse_args()

    original = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    optimized, changed = widen(original)
    args.asm.write_text("".join(optimized), encoding="utf-8")
    print(f"peephole: widened {changed} static generated JRs after code expansion")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
