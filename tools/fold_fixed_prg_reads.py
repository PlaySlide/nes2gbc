#!/usr/bin/env python3
"""Fold fixed absolute PRG reads for cartridges with immutable fixed PRG.

Mapper 0 (NROM) and mapper 3 (CNROM) never bank-switch CPU PRG. Therefore a
translated read from a compile-time absolute $8000-$FFFF address is literally a
ROM constant. Replacing the generic CPU-bus helper avoids a ROM-bank excursion
away from translated code and back for each such read.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path


def code(line: str) -> str:
    return line.split(";", 1)[0].strip()


def load_rom(path: Path) -> tuple[int, bytes] | None:
    data = path.read_bytes()
    if len(data) < 16 or data[:4] != b"NES\x1a":
        return None
    h = data[:16]
    mapper = (h[6] >> 4) | (h[7] & 0xF0)
    if (h[7] & 0x0C) == 0x08:
        mapper |= (h[8] & 0x0F) << 8
    if mapper not in (0, 3):
        return None

    # All currently supported NROM/CNROM test images use ordinary 16K units.
    # Refuse NES2 exponent/multiplier sizing rather than guessing.
    if (h[7] & 0x0C) == 0x08 and (h[9] & 0x0F) == 0x0F:
        return None
    prg_len = h[4] * 0x4000
    if prg_len not in (0x4000, 0x8000):
        return None
    start = 16 + (512 if (h[6] & 0x04) else 0)
    end = start + prg_len
    if end > len(data):
        return None
    return mapper, data[start:end]


def prg_byte(prg: bytes, addr: int) -> int:
    offset = addr - 0x8000
    if len(prg) == 0x4000:
        offset &= 0x3FFF
    else:
        offset &= 0x7FFF
    return prg[offset]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("asm", type=Path)
    parser.add_argument("rom", type=Path)
    args = parser.parse_args()

    loaded = load_rom(args.rom)
    if loaded is None:
        print("prg-fold: skipped (PRG is not supported fixed NROM/CNROM)")
        return 0
    mapper, prg = loaded

    lines = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    addr_re = re.compile(r"ld hl, \$([89A-Fa-f][0-9A-Fa-f]{3})")
    folded = 0

    for i in range(len(lines)):
        m = addr_re.fullmatch(code(lines[i]))
        if not m:
            continue
        j = i + 1
        while j < len(lines) and not code(lines[j]):
            j += 1
        if j >= len(lines) or code(lines[j]) != "call nes_cpu_read":
            continue

        addr = int(m.group(1), 16)
        value = prg_byte(prg, addr)
        indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
        lines[i] = (
            f"{indent}PROFILE_INC nes_profile_cpu_read\n"
            f"{indent}PROFILE_INC nes_profile_read_prg\n"
            f"{indent}; folded mapper {mapper} fixed PRG read ${addr:04X}\n"
            f"{indent}IF DEF(NES2GBC_DEBUG_TRACE)\n"
            f"{indent}ld a, ${addr >> 8:02X}\n"
            f"{indent}ld [nes_debug_bus_hi], a\n"
            f"{indent}ld a, ${addr & 0xFF:02X}\n"
            f"{indent}ld [nes_debug_bus_lo], a\n"
            f"{indent}ld a, ${value:02X}\n"
            f"{indent}ld [nes_debug_bus_value], a\n"
            f"{indent}ENDC\n"
            f"{indent}ld a, ${value:02X}\n"
        )
        lines[j] = f"{indent}; fixed PRG helper removed\n"
        folded += 1

    args.asm.write_text("".join(lines), encoding="utf-8")
    print(f"prg-fold: folded {folded} fixed absolute PRG reads")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
