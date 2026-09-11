#!/usr/bin/env python3
"""Shrink the exact CMP/CPX/CPY expansion emitted by the perf peephole.

GB SUB already gives us both the subtraction result and a borrow flag. 6502 C
for CMP is simply !borrow, so there is no reason to restore lhs and compare a
second time or branch to synthesize a 0/1 carry shadow.
"""

from __future__ import annotations

import argparse
from pathlib import Path


def code(line: str) -> str:
    return line.split(";", 1)[0].strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("asm", type=Path)
    args = parser.parse_args()

    lines = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    shrunk = 0

    wanted = [
        "PROFILE_INC nes_profile_compare",
        "ld d, a",
        "sub e",
        "ld c, a",
        "ld a, d",
        "cp e",
        "ld a, $00",
        "jr c, :+",
        "inc a",
        ":",
        "ldh [nes_c_shadow], a",
        "ld a, c",
        "ldh [nes_z_shadow], a",
        "ldh [nes_n_shadow], a",
    ]

    i = 0
    while i < len(lines):
        if code(lines[i]) != wanted[0]:
            i += 1
            continue

        idx = [i]
        j = i + 1
        for want in wanted[1:]:
            while j < len(lines) and not code(lines[j]):
                j += 1
            if j >= len(lines) or code(lines[j]) != want:
                idx = []
                break
            idx.append(j)
            j += 1
        if not idx:
            i += 1
            continue

        indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
        lines[i] = (
            f"{indent}PROFILE_INC nes_profile_compare\n"
            f"{indent}; exact CMP via GB borrow: 6502 C = !borrow\n"
            f"{indent}sub e\n"
            f"{indent}ld c, a\n"
            f"{indent}ccf\n"
            f"{indent}ld a, $00\n"
            f"{indent}rl a\n"
            f"{indent}ldh [nes_c_shadow], a\n"
            f"{indent}ld a, c\n"
            f"{indent}ldh [nes_z_shadow], a\n"
            f"{indent}ldh [nes_n_shadow], a\n"
        )
        for k in idx[1:]:
            lines[k] = f"{indent}; compare sequence folded\n"
        shrunk += 1
        i = j

    args.asm.write_text("".join(lines), encoding="utf-8")
    print(f"compare-fold: shrunk {shrunk} exact CMP/CPX/CPY expansions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
