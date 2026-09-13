#!/usr/bin/env python3
"""Remove generic bus checks from statically bounded indexed NES-RAM accesses.

For Absolute,X / Absolute,Y with a compile-time base <= $1F00, an 8-bit index
can never leave $0000-$1FFF. The emitter currently still generates a dynamic
`cp $20` fast-path test plus a generic bus fallback. That fallback is
unreachable.

When the mirrored 2 KiB offset is also <= $0700, the full 8-bit indexed range
cannot cross the NES RAM mirror boundary. In that case pre-map the base itself
to $C000-$C7FF and remove the later H-byte mirror remap as well. Preserve the
old remap's final `or $C0` host-flag normalization with one cheap `or $C0`
before the actual read/write; host carry/zero state is intentionally not left
stale.
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


def can_premap(base: int) -> bool:
    """True when base+0..255 stays within one 2 KiB NES RAM mirror window."""
    return base <= 0x1F00 and (base & 0x07FF) <= 0x0700


def premapped_base(base: int) -> int:
    return 0xC000 + (base & 0x07FF)


def trim_reads(lines: list[str]) -> tuple[int, int]:
    trimmed = 0
    premapped = 0
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
        if can_premap(base):
            # The entire indexed range maps linearly inside $C000-$C7FF, so map
            # the base up front and delete both the bus test and mirror remap.
            lines[i] = f"{ind}ld hl, ${premapped_base(base):04X} ; pre-mapped NES RAM mirror\n"
            lines[i + 7] = f"{ind}or $C0 ; preserve indexed-RAM C=0/Z=0 normalization\n"
            lines[i + 8] = f"{ind}ld a, [hl]\n"
            for j in range(i + 9, i + 18):
                lines[j] = f"{ind}; pre-mapped indexed RAM bus/remap removed\n"
            premapped += 1
        else:
            # Keep address addition and the existing `ld a,h` at i+7. Replace
            # the dynamic address-space test with guaranteed RAM mirror mapping.
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
    return trimmed, premapped


def trim_writes(lines: list[str]) -> tuple[int, int]:
    trimmed = 0
    premapped = 0
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
        if can_premap(base):
            # Preserve the stored value in C exactly as before. A is then free
            # for the old mirror-remap's host-flag normalization before restore.
            lines[i] = f"{ind}ld hl, ${premapped_base(base):04X} ; pre-mapped NES RAM mirror\n"
            lines[i + 9] = f"{ind}or $C0 ; preserve indexed-RAM C=0/Z=0 normalization\n"
            lines[i + 10] = f"{ind}ld a, c\n"
            lines[i + 11] = f"{ind}ld [hl], a\n"
            for j in range(i + 12, i + 22):
                lines[j] = f"{ind}; pre-mapped indexed RAM bus/remap removed\n"
            premapped += 1
        else:
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
    return trimmed, premapped


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("asm", type=Path)
    args = p.parse_args()

    lines = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    reads, pre_reads = trim_reads(lines)
    writes, pre_writes = trim_writes(lines)
    args.asm.write_text("".join(lines), encoding="utf-8")
    print(
        f"indexed-ram: removed dynamic bus checks from {reads} reads / {writes} writes; "
        f"pre-mapped {pre_reads}/{pre_writes} safe mirrored bases"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
