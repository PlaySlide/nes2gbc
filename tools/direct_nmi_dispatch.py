#!/usr/bin/env python3
"""Bypass the dynamic translated-PC dispatcher for the fixed NES NMI vector.

A delivered NES NMI always jumps to the cartridge's fixed NMI vector. In a full
translation that vector is already a statically linked `nes_XXXX` block, so the
ROM0 NMI entry does not need to switch to a dispatch-table bank, look up XXXX,
cache the result, then switch back to the translated code bank.

This pass changes only:

    nes_nmi_entry:
        ld hl, $XXXX
        jp nes_dispatch_hl

into the same known-bank transfer used by ordinary static cross-bank edges:

    nes_nmi_entry:
        ld a, BANK(nes_XXXX)
        ld hl, nes_XXXX
        jp nes_jump_known_hl_a

The optimization is applied only when `nes_XXXX:` is actually present in the
generated assembly. Development builds using MAX_BLOCKS therefore retain the
dynamic fallback automatically if the NMI block was sliced out.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path


def code(line: str) -> str:
    return line.split(";", 1)[0].strip()


def next_code(lines: list[str], start: int) -> int | None:
    for i in range(start, len(lines)):
        if code(lines[i]):
            return i
    return None


def optimize(lines: list[str]) -> tuple[bool, int | None]:
    try:
        entry = next(i for i, line in enumerate(lines) if code(line) == "nes_nmi_entry:")
    except StopIteration:
        return False, None

    load_i = next_code(lines, entry + 1)
    if load_i is None:
        return False, None
    m = re.fullmatch(r"ld hl, \$([0-9A-Fa-f]{4})", code(lines[load_i]))
    if not m:
        return False, None

    jump_i = next_code(lines, load_i + 1)
    if jump_i is None or code(lines[jump_i]) != "jp nes_dispatch_hl":
        return False, None

    target = int(m.group(1), 16)
    label = f"nes_{target:04X}:"
    if not any(code(line) == label for line in lines):
        # MAX_BLOCKS/dev slice: preserve the dynamic dispatcher fallback.
        return False, target

    ind = lines[load_i][: len(lines[load_i]) - len(lines[load_i].lstrip())]
    lines[load_i] = (
        f"{ind}; fixed NMI vector: bypass dynamic PC dispatch\n"
        f"{ind}ld a, BANK(nes_{target:04X})\n"
        f"{ind}ld hl, nes_{target:04X}\n"
    )
    lines[jump_i] = f"{ind}jp nes_jump_known_hl_a\n"
    return True, target


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("asm", type=Path)
    args = p.parse_args()

    lines = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    changed, target = optimize(lines)
    args.asm.write_text("".join(lines), encoding="utf-8")

    if changed:
        print(f"nmi-dispatch: direct known-bank jump to NES ${target:04X}")
    elif target is not None:
        print(f"nmi-dispatch: kept dynamic ${target:04X} fallback (target block not selected)")
    else:
        print("nmi-dispatch: no matching NMI entry")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
