#!/usr/bin/env python3
"""Remove provably dead Z/N stores before canonical BVC/BVS branches.

BVC/BVS continue to use the canonical emulated overflow bit from `nes_p` and
test mask $40 exactly as before. This pass only removes unrelated Z/N shadow
stores from the immediately preceding 6502 producer when existing CFG liveness
proves neither successor can observe them.

CFG parsing and inter-block liveness are reused from `dead_terminal_zn.py`, so
NMI poll prologues, dynamic exits, status-stack operations, and unknown control
flow remain conservative exactly as in the validated dead-shadow passes.
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
        if branch.mnemonic not in {"Bvc", "Bvs"}:
            continue

        # Keep this tied to the canonical overflow path. Do not optimize a site
        # whose generated branch no longer explicitly reloads P and masks V.
        branch_segment = "".join(lines[branch.line + 1 : block.end_i])
        if "ldh a, [nes_p]" not in branch_segment or "and $40" not in branch_segment:
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

        # BVC/BVS consumes only canonical V. Z/N matter solely after the branch.
        if z_idx is not None and not z_out[block.addr]:
            ind = lines[z_idx][: len(lines[z_idx]) - len(lines[z_idx].lstrip())]
            lines[z_idx] = f"{ind}; dead terminal Z shadow store removed before overflow branch\n"
            z_removed += 1

        if n_idx is not None and not n_out[block.addr]:
            ind = lines[n_idx][: len(lines[n_idx]) - len(lines[n_idx].lstrip())]
            lines[n_idx] = f"{ind}; dead terminal N shadow store removed before overflow branch\n"
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
        f"dead-overflow-zn: analyzed {eligible} canonical terminal BVC/BVS producers, "
        f"removed {zr} Z + {nr} N shadow stores"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
