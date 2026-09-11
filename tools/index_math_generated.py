#!/usr/bin/env python3
"""Conservative indexed-address peepholes for generated LR35902 code."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


def code(line: str) -> str:
    return line.split(";", 1)[0].strip()


def indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def fuse_recent_index_reload(lines: list[str]) -> int:
    """Drop an X/Y reload when A still equals the just-published X/Y value."""
    fused = 0
    for i in range(len(lines)):
        load = code(lines[i])
        if load not in {"ldh a, [nes_x]", "ldh a, [nes_y]"}:
            continue
        target = load.replace("ldh a, [", "ldh [").replace("]", "], a")
        k = i - 1
        while k >= 0 and code(lines[k]) in {
            "",
            "ldh [nes_z_shadow], a",
            "ldh [nes_n_shadow], a",
        }:
            k -= 1
        if k < 0 or code(lines[k]) != target:
            continue
        ind = indent_of(lines[i])
        lines[i] = f"{ind}; fused index reload: A still holds {target[5:10]}\n"
        fused += 1
    return fused


def trim_page_aligned_absolute_index(lines: list[str]) -> int:
    """For base $xx00 + 8-bit index, carry into H is impossible."""
    trimmed = 0
    base_re = re.compile(r"ld hl, \$([0-9A-Fa-f]{2})00")
    for i in range(len(lines) - 6):
        bm = base_re.fullmatch(code(lines[i]))
        if not bm:
            continue
        if code(lines[i + 1]) not in {"ldh a, [nes_x]", "ldh a, [nes_y]"}:
            continue
        expected = ["add l", "ld l, a", "jr nc, :+", "inc h", ":"]
        if [code(lines[i + 2 + n]) for n in range(5)] != expected:
            continue

        ind = indent_of(lines[i])
        # Keep the original LD HL so address-space selection is unchanged.
        # With L=$00, copying the 8-bit index into L is exactly base+index.
        # AND A reproduces the externally visible C=0/Z(index) state of ADD L;
        # H is irrelevant to NES status semantics but this avoids leaving stale
        # GB carry around for later peepholes.
        lines[i + 2] = f"{ind}ld l, a\n"
        lines[i + 3] = f"{ind}and a ; page-aligned absolute index, no carry\n"
        lines[i + 4] = f"{ind}; carry branch removed\n"
        lines[i + 5] = f"{ind}; high-byte increment impossible\n"
        lines[i + 6] = f"{ind}; page-aligned index label removed\n"
        trimmed += 1
    return trimmed


def trim_zero_page_zero_base(lines: list[str]) -> int:
    """`zp=$00 + X/Y` does not need an 8-bit immediate ADD."""
    trimmed = 0
    for i in range(len(lines) - 1):
        if code(lines[i]) not in {"ldh a, [nes_x]", "ldh a, [nes_y]"}:
            continue
        if code(lines[i + 1]) != "add $00":
            continue
        ind = indent_of(lines[i + 1])
        lines[i + 1] = f"{ind}and a ; zero-page base $00\n"
        trimmed += 1
    return trimmed


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("asm", type=Path)
    args = p.parse_args()
    lines = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)

    reloads = fuse_recent_index_reload(lines)
    pages = trim_page_aligned_absolute_index(lines)
    zp0 = trim_zero_page_zero_base(lines)

    args.asm.write_text("".join(lines), encoding="utf-8")
    print(
        f"index-math: fused {reloads} immediate X/Y reloads, "
        f"trimmed {pages} page-aligned absolute indexes, {zp0} zero-page-$00 adds"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
