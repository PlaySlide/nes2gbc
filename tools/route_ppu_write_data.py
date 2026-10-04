#!/usr/bin/env python3
"""Compile out the stitched $2007 fast path when it can never apply.

nes_ppu_write_data starts with a fast path for vertically mirrored stitched
(SMB-style) nametable writes (nes_mirroring == 1). When the generated code
sets nes_mirroring exactly once, to a constant other than 1, those tests are
pure overhead on every $2007 store (direct or via nes_ppu_cpu_write), so
define NES2GBC_NO_STITCH_WRITE_FASTPATH in generated_config.inc (rewritten by
every `make generate`).
Usage: route_ppu_write_data.py generated.asm generated_config.inc
"""
import re, sys

asm, cfg = sys.argv[1], sys.argv[2]
src = open(asm).read()
writes = re.findall(r"ld a, \$([0-9A-Fa-f]{2})\n\s*ld \[nes_mirroring\], a", src)
if src.count("[nes_mirroring], a") != 1 or len(writes) != 1 or int(writes[0], 16) == 1:
    print("route-ppu-write-data: mirroring may be vertical, fast path kept")
    sys.exit(0)
with open(cfg, "a") as f:
    f.write("DEF NES2GBC_NO_STITCH_WRITE_FASTPATH EQU 1 ; fixed non-vertical mirroring\n")
print("route-ppu-write-data: fixed non-vertical mirroring, stitched $2007 fast path compiled out")
