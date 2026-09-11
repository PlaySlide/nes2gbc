#!/usr/bin/env python3
"""Small release peepholes for generated LR35902 assembly.

This is intentionally conservative: it only removes an HRAM flag-shadow reload
when the immediately preceding emitted flag publication proves that A still
contains the same value.  That lets the following branch test use A directly
without changing the architectural Z/N shadows that later code may still read.

The prototype lives as a generated-assembly pass so we can measure it across
several games before baking the transformation into the Rust emitter.
"""

from __future__ import annotations

import argparse
from pathlib import Path


def _code(line: str) -> str:
    return line.split(";", 1)[0].strip()


def optimize_lines(lines: list[str]) -> tuple[list[str], int]:
    out = list(lines)
    fused = 0

    # Emitter sequence for any operation whose Z/N result is still in A:
    #   ldh [nes_z_shadow], a
    #   ldh [nes_n_shadow], a
    #   ; optional comments / blank lines
    #   ldh a, [nes_z_shadow]   or   ldh a, [nes_n_shadow]
    #   and a                   or   bit 7, a
    #
    # Both shadow stores preserve A, so the reload is redundant.  Keep the
    # shadow stores themselves: the 6502 flags remain architecturally live
    # after the branch and a later block may consume them.
    for i in range(len(out)):
        load = _code(out[i])
        if load not in {
            "ldh a, [nes_z_shadow]",
            "ldh a, [nes_n_shadow]",
        }:
            continue

        # The load must be consumed immediately as the corresponding branch
        # test.  This makes the pass insensitive to branch direction/target.
        j = i + 1
        while j < len(out) and not _code(out[j]):
            j += 1
        if j >= len(out):
            continue
        test = _code(out[j])
        if load.endswith("[nes_z_shadow]"):
            if test != "and a":
                continue
        else:
            if test != "bit 7, a":
                continue

        # Walk backward across comments/blank lines only.  We require the exact
        # final two flag publications from emit_update_nz; no helper call, load,
        # arithmetic op, or other possible A clobber may intervene.
        k = i - 1
        while k >= 0 and not _code(out[k]):
            k -= 1
        if k < 0 or _code(out[k]) != "ldh [nes_n_shadow], a":
            continue
        k -= 1
        while k >= 0 and not _code(out[k]):
            k -= 1
        if k < 0 or _code(out[k]) != "ldh [nes_z_shadow], a":
            continue

        # Preserve line count for readable generated listings while removing
        # the instruction from the assembler input.
        indent = out[i][: len(out[i]) - len(out[i].lstrip())]
        shadow = "Z" if "nes_z_shadow" in load else "N"
        out[i] = f"{indent}; fused {shadow} branch: A already holds flag result\n"
        fused += 1

    return out, fused


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("asm", type=Path)
    args = parser.parse_args()

    original = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    optimized, fused = optimize_lines(original)
    args.asm.write_text("".join(optimized), encoding="utf-8")
    print(f"peephole: fused {fused} immediate Z/N branch shadow reloads")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
