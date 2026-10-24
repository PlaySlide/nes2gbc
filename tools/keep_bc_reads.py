#!/usr/bin/env python3
"""Banked mappers: drop the push bc / pop bc around nes_cpu_read_hi[_view].

The superblock (zp),Y emitters keep cached X/Y in B/C across the PRG-table
fallback of an indexed read:

    push bc ; superblock (zp),Y: keep cached X/Y
    [ld a, $NN]
    call nes_cpu_read_hi | nes_cpu_read_hi_view
    pop bc

With NES2GBC_KEEP_BC_READS defined (runtime/cpu.asm), nes_cpu_read_hi and
nes_cpu_read_hi_view preserve B and C on every path: the PRG and PRG-RAM paths
never touch them, and the only paths that can ($2000-$5FFF through the generic
nes_cpu_read ladder: PPU/APU/controller reads) save BC themselves
(nes_cpu_read_keep_bc; RAM mirrors $0000-$1FFF use nes_cpu_read.ram, which
never touches BC). The pair is
then pure overhead (28 GB cycles per call), so remove it. Same values, same
flags (pop bc leaves AF alone), same side effects and order.

Only runs on banked-mapper builds (generated_config.inc has RAM_INTERP) and
appends the DEF to generated_config.inc; NES2GBC_KEEP_BC_READS=0 disables the
pass and the runtime change, giving byte-identical output to before.
"""
import os
import re
import sys
from pathlib import Path

CALL = re.compile(r"^call (nes_cpu_read_hi|nes_cpu_read_hi_view)$")
LDA = re.compile(r"^ld a, \$[0-9A-Fa-f]{2}$")


def code(l):
    return l.split(";", 1)[0].strip()


def main(path):
    if os.environ.get("NES2GBC_KEEP_BC_READS", "1") == "0":
        print("keep-bc-reads: disabled")
        return
    p = Path(path)
    cfg = p.parent / "generated_config.inc"
    if not cfg.exists() or "NES2GBC_RAM_INTERP" not in cfg.read_text():
        print("keep-bc-reads: not a banked-mapper build, skipped")
        return
    lines = p.read_text().splitlines(keepends=True)
    drop = set()
    n = 0
    for i, l in enumerate(lines):
        if code(l) != "push bc":
            continue
        # next code lines: optional ld a, imm; the call; then pop bc
        j = i + 1
        seq = []
        while j < len(lines) and len(seq) < 3:
            c = code(lines[j])
            if c:
                seq.append((j, c))
            elif lines[j].strip() == "" or not lines[j].lstrip().startswith(";"):
                break
            j += 1
        k = 0
        if k < len(seq) and LDA.match(seq[k][1]):
            k += 1
        if k + 1 < len(seq) and CALL.match(seq[k][1]) and seq[k + 1][1] == "pop bc":
            drop.add(i)
            drop.add(seq[k + 1][0])
            n += 1
    if n:
        p.write_text("".join(l for i, l in enumerate(lines) if i not in drop))
        with cfg.open("a") as f:
            f.write("DEF NES2GBC_KEEP_BC_READS EQU 1 ; nes_cpu_read_hi[_view] preserve BC (opt out: NES2GBC_KEEP_BC_READS=0)\n")
    print(f"keep-bc-reads: removed {n} push bc/pop bc pair(s) around nes_cpu_read_hi[_view]")


if __name__ == "__main__":
    main(sys.argv[1])
