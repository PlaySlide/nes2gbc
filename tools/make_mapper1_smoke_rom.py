#!/usr/bin/env python3
from pathlib import Path
import sys

out = Path(sys.argv[1] if len(sys.argv) > 1 else "ci-mapper1-smoke.nes")

header = bytearray(16)
header[:4] = b"NES\x1a"
header[4] = 4  # four 16 KiB PRG banks
header[5] = 0  # CHR RAM
header[6] = 0x10  # mapper 1 (MMC1)

banks = [bytearray([0x02] * 0x4000) for _ in range(4)]

def select_prg_code(bank_value: int) -> bytes:
    code = bytearray()
    for bit in range(5):
        code += bytes([
            0xA9, (bank_value >> bit) & 1,  # LDA #serial bit
            0x8D, 0x00, 0xE0,              # STA $E000
        ])
    code += bytes([0x4C, 0x00, 0x80])      # JMP $8000 after bank commit
    return bytes(code)

# Cycle through three switchable low banks; mode 3 keeps the last bank fixed high.
banks[0][:] = banks[0]
banks[0][:len(select_prg_code(1))] = select_prg_code(1)
banks[1][:len(select_prg_code(2))] = select_prg_code(2)
banks[2][:len(select_prg_code(0))] = select_prg_code(0)

fixed = banks[3]
fixed[0x0000:0x0008] = bytes([
    0xA9, 0x80,             # LDA #$80
    0x8D, 0x00, 0x80,       # STA $8000: MMC1 serial reset, force mode 3
    0x4C, 0x00, 0x80,       # JMP switchable low window
])
fixed[0x0100] = 0x40  # RTI at $C100
fixed[0x0101] = 0x40  # RTI at $C101
fixed[-6:] = bytes([
    0x00, 0xC1,
    0x00, 0xC0,
    0x01, 0xC1,
])

out.write_bytes(header + b"".join(banks))
print(out)
