#!/usr/bin/env python3
from pathlib import Path
import sys

out = Path(sys.argv[1] if len(sys.argv) > 1 else "ci-mapper2-crossbank.nes")

header = bytearray(16)
header[:4] = b"NES\x1a"
header[4] = 4
header[5] = 0
header[6] = 0x20

banks = [bytearray([0x02] * 0x4000) for _ in range(4)]

# Bank 0 entry: select bank 1, then jump to a DIFFERENT logical address.
banks[0][0x0000:0x0008] = bytes([
    0xA9, 0x01,
    0x8D, 0x00, 0x80,
    0x4C, 0x00, 0x90,
])

# Bank 1 intentionally has junk at $8000; valid post-switch code exists only
# at $9000. Per-bank reset-only discovery misses this without convergence.
banks[1][0x1000:0x1003] = bytes([0x4C, 0x00, 0x90])

fixed = banks[3]
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
