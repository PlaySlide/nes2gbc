#!/usr/bin/env python3
"""Tighten already-inlined 6502 stack sequences in generated LR35902.

This pass is intentionally semantic-neutral: it only substitutes shorter native
LR35902 forms for byte-for-byte equivalent stack accesses emitted by the earlier
peephole pass. The virtual 6502 stack/SP behavior and RTS dispatch are unchanged.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path


def code(line: str) -> str:
    return line.split(";", 1)[0].strip()


def indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def next_code(lines: list[str], start: int) -> int | None:
    i = start
    while i < len(lines):
        if code(lines[i]):
            return i
        i += 1
    return None


def tighten_immediate_stack_stores(lines: list[str]) -> int:
    """`ld a,$nn ; ld [hl],a` -> `ld [hl],$nn` inside static JSR pushes."""
    changed = 0
    imm_re = re.compile(r"ld a, \$([0-9A-Fa-f]{2})")
    for i in range(len(lines)):
        m = imm_re.fullmatch(code(lines[i]))
        if not m:
            continue
        j = next_code(lines, i + 1)
        if j is None or code(lines[j]) != "ld [hl], a":
            continue

        # Restrict to the exact static-JSR inliner region. Looking backward for
        # its marker avoids touching ordinary generated loads/stores.
        lo = max(0, i - 12)
        if not any("inline static 6502 JSR return push" in lines[k] for k in range(lo, i)):
            continue

        ind = indent_of(lines[i])
        lines[i] = f"{ind}ld [hl], ${m.group(1).upper()} ; immediate JSR stack byte\n"
        lines[j] = f"{ind}; accumulator load/store pair folded above\n"
        changed += 1
    return changed


def tighten_byte_pushes(lines: list[str]) -> int:
    """Use `ld [hl],e` for the value saved by the one-byte stack inliner."""
    changed = 0
    for i in range(len(lines)):
        if code(lines[i]) != "ld a, e":
            continue
        j = next_code(lines, i + 1)
        if j is None or code(lines[j]) != "ld [hl], a":
            continue
        lo = max(0, i - 10)
        if not any("inline nes_stack_push_a" in lines[k] for k in range(lo, i)):
            continue
        ind = indent_of(lines[i])
        lines[i] = f"{ind}ld [hl], e ; stored push value already lives in E\n"
        lines[j] = f"{ind}; redundant A move/store folded above\n"
        changed += 1
    return changed


def tighten_rts_pops(lines: list[str]) -> tuple[int, int]:
    """Load RTS low/high return bytes directly into C/H from [HL]."""
    low = 0
    high = 0
    for i in range(len(lines)):
        if code(lines[i]) != "ld a, [hl]":
            continue
        j = next_code(lines, i + 1)
        if j is None:
            continue
        lo = max(0, i - 12)
        if not any("inline 6502 RTS return pop" in lines[k] for k in range(lo, i)):
            continue

        ind = indent_of(lines[i])
        if code(lines[j]) == "ld c, a":
            lines[i] = f"{ind}ld c, [hl] ; RTS low byte direct\n"
            lines[j] = f"{ind}; redundant A-to-C move removed\n"
            low += 1
        elif code(lines[j]) == "ld h, a":
            # LR35902 reads [HL] using the old H, then writes H. The next RTS
            # instruction restores L from C, so changing H here is exactly the
            # same state transition as `ld a,[hl] / ld h,a`.
            lines[i] = f"{ind}ld h, [hl] ; RTS high byte direct\n"
            lines[j] = f"{ind}; redundant A-to-H move removed\n"
            high += 1
    return low, high


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("asm", type=Path)
    args = p.parse_args()

    lines = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    imm = tighten_immediate_stack_stores(lines)
    push = tighten_byte_pushes(lines)
    low, high = tighten_rts_pops(lines)
    args.asm.write_text("".join(lines), encoding="utf-8")
    print(
        f"stack-tight: {imm} immediate JSR bytes, {push} byte pushes, "
        f"{low}/{high} direct RTS low/high loads"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
