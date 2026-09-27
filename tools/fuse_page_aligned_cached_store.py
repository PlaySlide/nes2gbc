#!/usr/bin/env python3
"""Fuse exact page-aligned internal-RAM stores using resident X/Y.

The stateful emitter can carry NES X/Y in B/C, but an absolute-indexed store to
an aligned page still uses the generic address sequence and saves/restores A:

    ldh a, [nes_a]
    push af
    ldh a, [nes_y]
    ld c, a
    ld hl, $C200
    ld a, c
    add l
    ld l, a
    jr nc, :+
    inc h
:
    pop af
    ld [hl], a

For a $xx00 base the 8-bit index can never carry into H.  Because the PUSH/POP
already proves the address arithmetic's host flags are discarded, build L
directly from the resident index and defer the accumulator reload until the
store.  The virtual NES A/X/Y state and memory address are unchanged.

This pass intentionally matches only STA AbsoluteX/AbsoluteY into mirrored NES
internal RAM ($C000-$C7FF), with the exact stateful-emitter shape above.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

SOURCE_RE = re.compile(
    r"; \$[0-9A-Fa-f]{4}: \$[0-9A-Fa-f]{2} ([A-Za-z0-9_]+) ([A-Za-z0-9_]+)"
)
# Mirrored NES internal RAM occupies $C000-$C7FF. A page-aligned base is
# therefore exactly $C000, $C100, ... $C700.
BASE_RE = re.compile(r"ld hl, \$(C[0-7]00)$")


def code(line: str) -> str:
    return line.split(";", 1)[0].strip()


def indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def source_matches(lines: list[str], start: int, mode: str) -> bool:
    for i in range(start - 1, max(-1, start - 8), -1):
        m = SOURCE_RE.search(lines[i])
        if m:
            return m.group(1) == "Sta" and m.group(2) == mode
        c = code(lines[i])
        if c.startswith("SECTION ") or (c.startswith("nes_") and c.endswith(":")):
            break
    return False


def fuse(lines: list[str]) -> int:
    changed = 0
    i = 0
    while i + 12 < len(lines):
        if code(lines[i]) != "ldh a, [nes_a]" or code(lines[i + 1]) != "push af":
            i += 1
            continue

        idx_load = code(lines[i + 2])
        if idx_load == "ldh a, [nes_x]":
            reg = "b"
            mode = "AbsoluteX"
        elif idx_load == "ldh a, [nes_y]":
            reg = "c"
            mode = "AbsoluteY"
        else:
            i += 1
            continue

        expected = [
            f"ld {reg}, a",
            None,  # aligned internal-RAM base
            f"ld a, {reg}",
            "add l",
            "ld l, a",
            "jr nc, :+",
            "inc h",
            ":",
            "pop af",
            "ld [hl], a",
        ]
        actual = [code(lines[i + 3 + n]) for n in range(10)]
        if actual[0] != expected[0] or BASE_RE.fullmatch(actual[1]) is None:
            i += 1
            continue
        if actual[2:] != expected[2:]:
            i += 1
            continue
        if not source_matches(lines, i, mode):
            i += 1
            continue

        ind = indent_of(lines[i])
        lines[i] = f"{ind}; defer canonical A reload until aligned indexed store\n"
        lines[i + 1] = f"{ind}; native AF save removed: address setup no longer clobbers A\n"
        # Keep the index seed/load and LD HL base exactly where they are.
        lines[i + 5] = f"{ind}ld l, {reg} ; page-aligned cached {reg.upper()} index\n"
        lines[i + 6] = f"{ind}; aligned low-byte add removed\n"
        lines[i + 7] = f"{ind}; aligned low-byte copy folded above\n"
        lines[i + 8] = f"{ind}; aligned carry branch removed\n"
        lines[i + 9] = f"{ind}; aligned high-byte increment impossible\n"
        lines[i + 10] = f"{ind}; aligned carry label removed\n"
        lines[i + 11] = f"{ind}ldh a, [nes_a] ; reload A immediately before store\n"
        changed += 1
        i += 13

    return changed


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("asm", type=Path)
    args = p.parse_args()

    lines = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    changed = fuse(lines)
    args.asm.write_text("".join(lines), encoding="utf-8")
    print(f"aligned-store: fused {changed} page-aligned cached X/Y internal-RAM store(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
