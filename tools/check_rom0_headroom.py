#!/usr/bin/env python3
"""Report ROM0 free bytes from an rgblink map and fail below a margin.

Usage: check_rom0_headroom.py [runtime/build/runtime.map] [--min N]
APU=1 is the default build and ROM0 is the tight bank (hot runtime + APU);
run this after `make -C runtime` so growth is caught before it overflows a
real game (e.g. SMB with APU=1, which has a few hundred bytes to spare).
"""
import re, sys

args = [a for a in sys.argv[1:] if not a.startswith("--min")]
path = args[0] if args else "runtime/build/runtime.map"
minimum = 0
for i, a in enumerate(sys.argv):
    if a == "--min":
        minimum = int(sys.argv[i + 1])
        args = [x for x in args if x != sys.argv[i + 1]]
text = open(path).read()
m = re.search(r"^ROM0 bank #0:.*?TOTAL EMPTY: \$([0-9A-Fa-f]+) bytes", text, re.S | re.M)
if not m:
    sys.exit(f"check_rom0_headroom: no ROM0 summary in {path}")
free = int(m.group(1), 16)
print(f"ROM0 free: {free} bytes")
if free < minimum:
    sys.exit(f"ROM0 headroom {free} < required {minimum}")
