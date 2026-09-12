#!/usr/bin/env python3
"""Collapse same-bank conditional branch + JP pairs in generated LR35902.

The static emitter represents a 6502 conditional branch as:

    jr <skip-cond>, :+
    jp nes_XXXX
:

when the translated target is in the same ROM bank but is not in short-JR
range (forward targets are the common case). LR35902 has a native conditional
absolute JP, so the sequence is exactly equivalent to:

    jp <take-cond>, nes_XXXX

This keeps the host flags untouched and changes no 6502 architectural state.
Taken branches save the extra conditional-JR hop; not-taken branches retain the
same condition semantics. Cross-bank transfers are intentionally not matched.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

SKIP_RE = re.compile(r"jr (z|nz), :\+")
TARGET_RE = re.compile(r"jp (nes_[0-9A-Fa-f]{4})")


def code(line: str) -> str:
    return line.split(";", 1)[0].strip()


def next_code(lines: list[str], start: int) -> int | None:
    i = start
    while i < len(lines):
        if code(lines[i]):
            return i
        i += 1
    return None


def optimize(lines: list[str]) -> int:
    collapsed = 0
    for i in range(len(lines)):
        m = SKIP_RE.fullmatch(code(lines[i]))
        if not m:
            continue

        j = next_code(lines, i + 1)
        if j is None:
            continue
        tm = TARGET_RE.fullmatch(code(lines[j]))
        if not tm:
            continue

        k = next_code(lines, j + 1)
        if k is None or code(lines[k]) != ":":
            continue

        # Original: skip target when condition m.group(1) is true.
        # Therefore jump to target on the inverse condition.
        take = "nz" if m.group(1) == "z" else "z"
        ind = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
        lines[i] = f"{ind}jp {take}, {tm.group(1)} ; collapsed same-bank conditional target\n"
        lines[j] = f"{ind}; redundant unconditional target jump removed\n"
        collapsed += 1

    return collapsed


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("asm", type=Path)
    args = p.parse_args()

    lines = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    n = optimize(lines)
    args.asm.write_text("".join(lines), encoding="utf-8")
    print(f"cond-jp: collapsed {n} same-bank conditional JP pairs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
