#!/usr/bin/env python3
"""Route generic reads whose inline RAM test already failed to nes_cpu_read_hi.

emit_dynamic_cpu_read_hl emits
    ld a, h / cp $20 / jr nc, :+ / <RAM mirror read> / jr :++ / : / call nes_cpu_read
so at that `call nes_cpu_read`, H >= $20. nes_cpu_read_hi skips the address
ladder for PRG ($8000+) and otherwise defers to nes_cpu_read. Runs last.
"""
import sys
from pathlib import Path

def code(l):
    return l.split(";", 1)[0].strip()

def main(path):
    p = Path(path)
    lines = p.read_text().splitlines(keepends=True)
    n = 0
    for i in range(2, len(lines)):
        if code(lines[i]) == "call nes_cpu_read" and code(lines[i - 1]) == ":" and code(lines[i - 2]) == "jr :++":
            ind = lines[i][:len(lines[i]) - len(lines[i].lstrip())]
            lines[i] = f"{ind}call nes_cpu_read_hi ; inline RAM test failed: H >= $20\n"
            n += 1
    p.write_text("".join(lines))
    print(f"fast-nonram-reads: {n} post-RAM-test reads routed to nes_cpu_read_hi")

if __name__ == "__main__":
    main(sys.argv[1])
