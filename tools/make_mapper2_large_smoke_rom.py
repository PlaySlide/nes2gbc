#!/usr/bin/env python3
from pathlib import Path
import sys

out = Path(sys.argv[1] if len(sys.argv) > 1 else "ci-mapper2-large.nes")

BANKS = 8
BLOCKS = 1000

header = bytearray(16)
header[:4] = b"NES\x1a"
header[4] = BANKS
header[5] = 0
header[6] = 0x20  # mapper 2

banks = [bytearray([0x02] * 0x4000) for _ in range(BANKS)]

# Give every switchable bank the same 1000-PC JMP chain. This deliberately
# creates thousands of banked block variants/canonical stubs and a generated
# assembly file in the same size class as real commercial UxROM games.
for bank in range(BANKS - 1):
    b = banks[bank]
    for i in range(BLOCKS):
        off = i * 3
        pc = 0x8000 + off
        target = 0x8000 + ((i + 1) % BLOCKS) * 3
        b[off:off+3] = bytes([0x4C, target & 0xFF, target >> 8])

fixed = banks[-1]
fixed[0x0000:0x0008] = bytes([
    0xA9, 0x00,
    0x8D, 0x00, 0x80,
    0x4C, 0x00, 0x80,
])
fixed[0x0100] = 0x40
fixed[0x0101] = 0x40
fixed[-6:] = bytes([
    0x00, 0xC1,
    0x00, 0xC0,
    0x01, 0xC1,
])

out.write_bytes(header + b"".join(banks))
print(out)
