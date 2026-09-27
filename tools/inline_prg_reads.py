#!/usr/bin/env python3
"""Specialize the PRG path of generic reads emitted after a failed RAM test.

`call nes_cpu_read_hi` (tools/fast_nonram_reads.py) is reached with H >= $20.
For H >= $80 the runtime either reads the WRAMX PRG mirror (16 KiB PRG) or
maps ROMX bank 1/2, reads, and restores the translated-code bank.
* 16 KiB PRG: the WRAMX mirror read is inlined (same result and returned
  flags, `or $D0`: NZ, NC); $2000-$7FFF still calls nes_cpu_read.
* 32 KiB PRG: the bank switch must run from ROM0 (the caller lives in
  ROMX), so the site calls nes_cpu_read_hi32, which skips the runtime
  16K-mirror test.
"""
import re, sys
from pathlib import Path


def main(path):
    p = Path(path)
    text = p.read_text()
    m = re.search(r"ld a, \$([0-9A-Fa-f]{2})\s*\n\s*ld \[nes_prg_16k_mirror\], a", text)
    if not m:
        print("inline-prg-reads: PRG layout unknown; skipped")
        return
    mirror = int(m.group(1), 16) != 0
    out = []
    n = 0
    for l in text.splitlines(keepends=True):
        if l.split(";", 1)[0].strip() == "call nes_cpu_read_hi":
            n += 1
            if not mirror:
                out.append("    call nes_cpu_read_hi32 ; 32K PRG: no mirror test\n")
                continue
            k = f".iprg{n}"
            seq = ["bit 7, h", f"jr z, {k}s",
                   "ld a, h", "and $30", "swap a", "add $02", "ldh [rSVBK], a",
                   "ld a, h", "and $0F", "or $D0", "ld h, a", "ld a, [hl]", f"jr {k}d"]
            out += ["    " + x + " ; inlined PRG mirror read\n" for x in seq]
            out.append(f"{k}s:\n")
            out.append("    call nes_cpu_read ; $2000-$7FFF\n")
            out.append(f"{k}d:\n")
            continue
        out.append(l)
    p.write_text("".join(out))
    print(f"inline-prg-reads: {n} sites ({'16K mirror inlined' if mirror else '32K via nes_cpu_read_hi32'})")


if __name__ == "__main__":
    main(sys.argv[1])
