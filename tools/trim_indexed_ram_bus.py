#!/usr/bin/env python3
"""Remove generic bus checks from statically bounded indexed NES-RAM accesses.

For Absolute,X / Absolute,Y with a compile-time base <= $1F00, an 8-bit index
can never leave $0000-$1FFF. The emitter currently still generates a dynamic
`cp $20` fast-path test plus a generic bus fallback. That fallback is
unreachable. Keep the exact NES 2 KiB mirror mapping (`H &= 7; H |= $C0`) and
remove only the impossible test/fallback.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path


def code(line: str) -> str:
    return line.split(";", 1)[0].strip()


def indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


BASE_RE = re.compile(r"ld hl, \$([0-9A-Fa-f]{4})")
INDEX_LOADS = {"ldh a, [nes_x]", "ldh a, [nes_y]"}


def trim_reads(lines: list[str]) -> int:
    trimmed = 0
    expected_tail = [
        "add l",
        "ld l, a",
        "jr nc, :+",
        "inc h",
        ":",
        "ld a, h",
        "cp $20",
        "jr nc, :+",
        "and $07",
        "or $C0",
        "ld h, a",
        "ld a, [hl]",
        "jr :++",
        ":",
        "call nes_cpu_read",
        ":",
    ]

    i = 0
    while i + 17 < len(lines):
        m = BASE_RE.fullmatch(code(lines[i]))
        if not m or code(lines[i + 1]) not in INDEX_LOADS:
            i += 1
            continue
        base = int(m.group(1), 16)
        if base > 0x1F00:
            i += 1
            continue
        if [code(lines[i + 2 + n]) for n in range(len(expected_tail))] != expected_tail:
            i += 1
            continue

        ind = indent_of(lines[i])
        # Keep address addition and the existing `ld a,h` at i+7. Replace the
        # dynamic address-space test with the guaranteed internal-RAM mapping.
        repl = [
            "and $07",
            "or $C0",
            "ld h, a",
            "ld a, [hl]",
        ]
        for n, insn in enumerate(repl):
            lines[i + 8 + n] = f"{ind}{insn}\n"
        for j in range(i + 12, i + 18):
            lines[j] = f"{ind}; unreachable indexed RAM bus fallback removed\n"
        trimmed += 1
        i += 18
    return trimmed


def trim_writes(lines: list[str]) -> int:
    trimmed = 0
    expected_tail = [
        "add l",
        "ld l, a",
        "jr nc, :+",
        "inc h",
        ":",
        "pop af",
        "ld c, a",
        "ld a, h",
        "cp $20",
        "jr nc, :+",
        "and $07",
        "or $C0",
        "ld h, a",
        "ld a, c",
        "ld [hl], a",
        "jr :++",
        ":",
        "ld a, c",
        "call nes_cpu_write",
        ":",
    ]

    i = 1
    while i + 21 < len(lines):
        if code(lines[i - 1]) != "push af":
            i += 1
            continue
        m = BASE_RE.fullmatch(code(lines[i]))
        if not m or code(lines[i + 1]) not in INDEX_LOADS:
            i += 1
            continue
        base = int(m.group(1), 16)
        if base > 0x1F00:
            i += 1
            continue
        if [code(lines[i + 2 + n]) for n in range(len(expected_tail))] != expected_tail:
            i += 1
            continue

        ind = indent_of(lines[i])
        # A already contains H at i+9. Preserve the pushed value in C exactly
        # as the emitter does, but remove the impossible non-RAM branch.
        repl = [
            "and $07",
            "or $C0",
            "ld h, a",
            "ld a, c",
            "ld [hl], a",
        ]
        for n, insn in enumerate(repl):
            lines[i + 10 + n] = f"{ind}{insn}\n"
        for j in range(i + 15, i + 22):
            lines[j] = f"{ind}; unreachable indexed RAM bus fallback removed\n"
        trimmed += 1
        i += 22
    return trimmed


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("asm", type=Path)
    args = p.parse_args()

    lines = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    reads = trim_reads(lines)
    writes = trim_writes(lines)
    args.asm.write_text("".join(lines), encoding="utf-8")
    print(
        f"indexed-ram: removed dynamic bus checks from {reads} reads / {writes} writes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
