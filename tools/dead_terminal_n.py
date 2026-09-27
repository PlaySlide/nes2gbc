#!/usr/bin/env python3
"""Remove provably dead terminal Z/N stores before fused BMI/BPL branches.

`dead_terminal_zn.py` already proves inter-block Z/N liveness for fused BEQ/BNE
producers. This companion pass reuses that exact CFG analysis for BMI/BPL when
the earlier peephole has proved that host A still carries the producer result.

The fused BMI/BPL consumes N directly from host A/GB flags, so canonical Z/N
shadow stores are needed only after the branch. A store is removed only when the
existing liveness analysis proves every successor kills that flag before any
possible read. NMI poll prologues, dynamic exits, stack/status operations and
unknown control flow remain conservative exactly as in the original pass.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from dead_terminal_zn import ZN_WRITERS, code, liveness, parse_blocks


def optimize(lines: list[str]) -> tuple[int, int, int]:
    blocks = parse_blocks(lines)
    _, z_out = liveness(blocks, "z")
    _, n_out = liveness(blocks, "n")

    eligible = 0
    z_removed = 0
    n_removed = 0

    for block in blocks.values():
        if len(block.insns) < 2:
            continue

        branch = block.insns[-1]
        if branch.mnemonic not in {"Bmi", "Bpl"}:
            continue

        branch_segment = "".join(lines[branch.line + 1 : block.end_i])
        if "fused N branch: A already holds flag result" not in branch_segment:
            continue

        producer = block.insns[-2]
        if producer.mnemonic not in ZN_WRITERS:
            continue

        eligible += 1
        z_idx = None
        n_idx = None
        for j in range(producer.line + 1, branch.line):
            c = code(lines[j])
            if c == "ldh [nes_z_shadow], a":
                z_idx = j
            elif c == "ldh [nes_n_shadow], a":
                n_idx = j

        # The fused branch consumes N from the still-live producer result.
        # Canonical Z and N are therefore needed only after the branch.
        if z_idx is not None and not z_out[block.addr]:
            ind = lines[z_idx][: len(lines[z_idx]) - len(lines[z_idx].lstrip())]
            lines[z_idx] = f"{ind}; dead terminal Z shadow store removed before fused N branch\n"
            z_removed += 1

        if n_idx is not None and not n_out[block.addr]:
            ind = lines[n_idx][: len(lines[n_idx]) - len(lines[n_idx].lstrip())]
            lines[n_idx] = f"{ind}; dead terminal N shadow store removed before fused N branch\n"
            n_removed += 1

    return eligible, z_removed, n_removed


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("asm", type=Path)
    args = p.parse_args()

    lines = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    eligible, zr, nr = optimize(lines)
    args.asm.write_text("".join(lines), encoding="utf-8")
    print(
        f"dead-n: analyzed {eligible} fused terminal BMI/BPL producers, "
        f"removed {zr} Z + {nr} N shadow stores"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
