#!/usr/bin/env python3
from pathlib import Path
import sys

out = Path(sys.argv[1] if len(sys.argv) > 1 else "ci-smoke.nes")

header = bytearray(16)
header[:4] = b"NES\x1a"
header[4] = 1  # 16 KiB PRG
header[5] = 1  # 8 KiB CHR

prg = bytearray([0xEA] * 0x4000)

# $8000: simple loop exercising immediate load, zero-page store, increment, compare, branch.
code = bytes([
    0xA9, 0x00,       # LDA #$00
    0x85, 0x00,       # STA $00
    0xE6, 0x00,       # INC $00
    0xA5, 0x00,       # LDA $00
    0xC9, 0x10,       # CMP #$10
    0xD0, 0xF8,       # BNE $8004
    0x4C, 0x00, 0x80, # JMP $8000
])
prg[:len(code)] = code

# Tiny NMI and IRQ handlers.
prg[0x10] = 0x40  # RTI at $8010
prg[0x11] = 0x40  # RTI at $8011

# NMI, RESET, IRQ vectors.
prg[-6:] = bytes([
    0x10, 0x80,
    0x00, 0x80,
    0x11, 0x80,
])

chr_data = bytes(0x2000)
out.write_bytes(header + prg + chr_data)
print(out)
