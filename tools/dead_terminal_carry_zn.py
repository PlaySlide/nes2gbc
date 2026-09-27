#!/usr/bin/env python3
"""Remove provably dead Z/N stores before canonical BCC/BCS branches.

This deliberately does NOT reuse LR35902 carry. BCC/BCS continue to reload the
canonical `nes_c_shadow` value and perform the existing `and a` normalization.
The only optimization here is unrelated bookkeeping: when the immediately
preceding 6502 instruction publishes Z/N shadows that no successor can observe,
remove those dead stores.

CFG parsing and inter-block liveness are reused from `dead_terminal_zn.py`.
NMI poll prologues, dynamic exits, status-stack operations, and unknown control
flow therefore remain conservative exactly as in the validated dead-Z/N passes.
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
        if branch.mnemonic not in {"Bcc", "Bcs"}:
            continue

        # Keep this pass tied to the canonical carry path. The prior host-carry
        # experiment was invalid; never optimize a carry branch that no longer
        # explicitly reloads the emulated carry byte.
        branch_segment = "".join(lines[branch.line + 1 : block.end_i])
        if "nes_c_shadow" not in branch_segment or "and a" not in branch_segment:
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

        # BCC/BCS consumes only canonical C. Z/N matter solely after the branch,
        # so remove either publication only when all successors kill it first.
        if z_idx is not None and not z_out[block.addr]:
            ind = lines[z_idx][: len(lines[z_idx]) - len(lines[z_idx].lstrip())]
            lines[z_idx] = f"{ind}; dead terminal Z shadow store removed before carry branch\n"
            z_removed += 1

        if n_idx is not None and not n_out[block.addr]:
            ind = lines[n_idx][: len(lines[n_idx]) - len(lines[n_idx].lstrip())]
            lines[n_idx] = f"{ind}; dead terminal N shadow store removed before carry branch\n"
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
        f"dead-carry-zn: analyzed {eligible} canonical terminal BCC/BCS producers, "
        f"removed {zr} Z + {nr} N shadow stores"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
