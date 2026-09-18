#!/usr/bin/env python3
"""Reuse the final INX/INY/DEX/DEY value through an immediate Z branch.

The stateful emitter can end an index step followed by BEQ/BNE like this:

    inc c
    ld a, c
    ldh [nes_z_shadow], a
    ldh [nes_n_shadow], a
    ld a, c
    ldh [nes_y], a
    ; ... BNE ...
    ldh a, [nes_z_shadow]
    and a
    jp nz, nes_TARGET

The first `ld a,c` already leaves A equal to both the architectural index and
`nes_z_shadow`.  The two HRAM stores do not alter A, and the canonical index
store does not alter A either.  Therefore the second register move and the
shadow reload are redundant.  Keep `and a` deliberately: besides testing Z it
preserves the existing host-flag normalization used by later generated code.

Match only this exact generated shape for INX/INY/DEX/DEY immediately followed
by BEQ/BNE.  Canonical X/Y and Z/N publication remain unchanged.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

SOURCE_RE = re.compile(
    r"; \$[0-9A-Fa-f]{4}: \$[0-9A-Fa-f]{2} ([A-Za-z0-9_]+) ([A-Za-z0-9_]+)"
)

STEP_INFO = {
    "Inx": ("inc b", "ld a, b", "nes_x"),
    "Iny": ("inc c", "ld a, c", "nes_y"),
    "Dex": ("dec b", "ld a, b", "nes_x"),
    "Dey": ("dec c", "ld a, c", "nes_y"),
}


def code(line: str) -> str:
    return line.split(";", 1)[0].strip()


def indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def fuse(lines: list[str]) -> tuple[int, int, int]:
    fused = 0
    beq = 0
    bne = 0
    i = 0

    while i + 10 < len(lines):
        sm = SOURCE_RE.search(lines[i])
        if sm is None or sm.group(2) != "Implied":
            i += 1
            continue

        info = STEP_INFO.get(sm.group(1))
        if info is None:
            i += 1
            continue
        step, move, canonical = info

        expected_prefix = [
            step,
            move,
            "ldh [nes_z_shadow], a",
            "ldh [nes_n_shadow], a",
            move,
            f"ldh [{canonical}], a",
        ]
        if [code(lines[i + 1 + n]) for n in range(6)] != expected_prefix:
            i += 1
            continue

        bm = SOURCE_RE.search(lines[i + 7])
        if bm is None or bm.group(2) != "Relative" or bm.group(1) not in {"Beq", "Bne"}:
            i += 1
            continue

        branch = bm.group(1)
        cond = "z" if branch == "Beq" else "nz"
        if code(lines[i + 8]) != "ldh a, [nes_z_shadow]":
            i += 1
            continue
        if code(lines[i + 9]) != "and a":
            i += 1
            continue
        if not re.fullmatch(rf"(?:jp|jr) {cond}, nes_[0-9A-Fa-f]{{4}}", code(lines[i + 10])):
            i += 1
            continue

        ind = indent_of(lines[i + 5])
        lines[i + 5] = f"{ind}; terminal index value already remains in A\n"
        lines[i + 8] = f"{ind}; terminal Z branch reuses index value still in A\n"
        fused += 1
        beq += int(branch == "Beq")
        bne += int(branch == "Bne")
        i += 11

    return fused, beq, bne


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("asm", type=Path)
    args = p.parse_args()

    lines = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    fused, beq, bne = fuse(lines)
    args.asm.write_text("".join(lines), encoding="utf-8")
    print(
        f"index-branch-value: fused {fused} terminal index value path(s) "
        f"({beq} BEQ / {bne} BNE); kept canonical X/Y and Z/N publication"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
