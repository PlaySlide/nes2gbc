#!/usr/bin/env python3
from pathlib import Path
import sys

out = Path(sys.argv[1] if len(sys.argv) > 1 else "ci-mapper2-smoke.nes")

header = bytearray(16)
header[:4] = b"NES\x1a"
header[4] = 4  # four 16 KiB PRG banks
header[5] = 1  # 8 KiB CHR
header[6] = 0x20  # mapper 2 (UxROM)

banks = [bytearray([0x02] * 0x4000) for _ in range(4)]  # illegal 6502 padding: never mistaken for code

# Each switchable bank selects the next bank, then redispatches $8000.
# If translated-PC dispatch ignores the mapper bank this becomes the wrong
# native block immediately, so the generated assembly must contain bank-aware
# canonical stubs for this address.
for bank in range(3):
    next_bank = bank + 1
    banks[bank][:8] = bytes([
        0xA9, next_bank,       # LDA #next_bank
        0x8D, 0x00, 0x80,     # STA $8000 (UxROM bank select)
        0x4C, 0x00, 0x80,     # JMP $8000 in the newly selected bank
    ])

# The final physical bank is also the fixed $C000-$FFFF bank. When selected
# into the lower window it jumps back to fixed code; at $C000 reset selects
# bank 0 and enters the switchable window.
banks[3][:3] = bytes([0x4C, 0x10, 0xC0])  # lower $8000 -> fixed $C010
fixed = banks[3]
fixed[0x0000:0x0008] = bytes([
    0xA9, 0x00,             # LDA #0
    0x8D, 0x00, 0x80,       # STA $8000
    0x4C, 0x00, 0x80,       # JMP $8000
])
fixed[0x0010:0x0013] = bytes([0x4C, 0x10, 0xC0])  # stable fixed-bank loop
fixed[0x0100] = 0x40  # RTI at $C100
fixed[0x0101] = 0x40  # RTI at $C101

fixed[-6:] = bytes([
    0x00, 0xC1,  # NMI
    0x00, 0xC0,  # RESET
    0x01, 0xC1,  # IRQ
])

chr_data = bytes(0x2000)
out.write_bytes(header + b"".join(banks) + chr_data)
print(out)
