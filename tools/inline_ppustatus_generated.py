#!/usr/bin/env python3
"""Inline exact fixed $2002/PPUSTATUS reads in generated code.

The earlier fixed-PPU peephole already bypasses the generic register dispatcher,
but hot sprite-0 polling loops still CALL/RET through nes_ppu_cpu_read.status on
every iteration.  This pass copies that handler body into the translated site.
Semantics stay identical: vblank/sprite-0 synthesis, status clear, and the
$2005/$2006 latch reset are all preserved.
"""

from __future__ import annotations

import argparse
from pathlib import Path


def code(line: str) -> str:
    return line.split(";", 1)[0].strip()


def indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def inline_status_reads(lines: list[str]) -> int:
    count = 0
    for i, line in enumerate(lines):
        if code(line) != "call nes_ppu_cpu_read.status":
            continue

        ind = indent_of(line)
        tag = f"nes_inline_ppustatus_{i}"
        visible = f"{tag}_visible"
        base = f"{tag}_base"
        ready = f"{tag}_ready"

        lines[i] = (
            f"{ind}; inline exact PPUSTATUS ($2002) read\n"
            f"{ind}ldh a, [rLY]\n"
            f"{ind}cp 144\n"
            f"{ind}jr c, {visible}\n"
            f"{ind}ld a, [nes_ppu_status]\n"
            f"{ind}and $3F\n"
            f"{ind}or $80\n"
            f"{ind}jr {ready}\n"
            f"{visible}:\n"
            f"{ind}ld b, a\n"
            f"{ind}ld a, [nes_ppu_status]\n"
            f"{ind}and $3F\n"
            f"{ind}ld e, a\n"
            f"{ind}ldh a, [nes_split_line]\n"
            f"{ind}ld c, a\n"
            f"{ind}ld a, b\n"
            f"{ind}cp c\n"
            f"{ind}jr c, {base}\n"
            f"{ind}ld a, [nes_ppumask]\n"
            f"{ind}and $18\n"
            f"{ind}cp $18\n"
            f"{ind}jr nz, {base}\n"
            f"{ind}ld a, [nes_oam_ram]\n"
            f"{ind}cp $EF\n"
            f"{ind}jr nc, {base}\n"
            f"{ind}ld a, e\n"
            f"{ind}or $40\n"
            f"{ind}jr {ready}\n"
            f"{base}:\n"
            f"{ind}ld a, e\n"
            f"{ready}:\n"
            f"{ind}ld e, a\n"
            f"{ind}and $7F\n"
            f"{ind}ld [nes_ppu_status], a\n"
            f"{ind}xor a\n"
            f"{ind}ld [nes_ppu_latch], a\n"
            f"{ind}ld a, e\n"
        )
        count += 1

    return count


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("asm", type=Path)
    args = p.parse_args()

    lines = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    count = inline_status_reads(lines)
    args.asm.write_text("".join(lines), encoding="utf-8")
    print(f"ppustatus-inline: inlined {count} fixed $2002 read sites")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
