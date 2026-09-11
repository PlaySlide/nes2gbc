#!/usr/bin/env python3
"""Fuse specialized $2002/AND #$40 polling into the following Z branch.

The sprite-0 specialization already guarantees A is exactly $00 or $40.  The
original 6502 AND #$40 therefore does not change A; after the earlier branch
peepholes it only exists to recreate host Z for the immediately following
BEQ/BNE.  Preserve that Z directly in the specialized status paths instead and
remove the redundant AND.  Canonical NES A/Z/N shadow stores remain untouched.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path


INSN_RE = re.compile(r"; \$[0-9A-Fa-f]{4}: \$[0-9A-Fa-f]{2} (\w+) (\w+)")
COND_JR_RE = re.compile(r"jr (?:z|nz), .+")


def code(line: str) -> str:
    return line.split(";", 1)[0].strip()


def is_insn_comment(line: str) -> bool:
    return INSN_RE.search(line.strip()) is not None


def next_insn(lines: list[str], start: int) -> int | None:
    i = start
    while i < len(lines):
        if is_insn_comment(lines[i]):
            return i
        i += 1
    return None


def fuse(lines: list[str]) -> int:
    fused = 0
    i = 0
    while i < len(lines):
        if "specialized $2002 -> AND #$40 sprite-0 poll" not in lines[i]:
            i += 1
            continue

        # Find the generated hit-path tail. `or $40` establishes Z=0.  Change
        # the subsequent latch clear from XOR A (which would destroy Z) to a
        # flag-preserving immediate load.  The final LD A,$40 also preserves Z.
        block_end = next_insn(lines, i + 1)
        if block_end is None:
            break

        changed_hit_tail = False
        for j in range(i, block_end - 3):
            if (
                code(lines[j]) == "ld [nes_ppu_status], a"
                and code(lines[j + 1]) == "xor a"
                and code(lines[j + 2]) == "ld [nes_ppu_latch], a"
                and code(lines[j + 3]) == "ld a, $40"
            ):
                ind = lines[j + 1][: len(lines[j + 1]) - len(lines[j + 1].lstrip())]
                lines[j + 1] = f"{ind}ld a, $00 ; preserve sprite-0 hit Z=0 while clearing latch\n"
                changed_hit_tail = True
                break
        if not changed_hit_tail:
            i = block_end
            continue

        # The immediately following 6502 instruction must be the original
        # AND #$40 that the specialization was built around.
        and_insn = block_end
        m = INSN_RE.search(lines[and_insn].strip())
        if m is None or m.group(1) != "And" or m.group(2) != "Immediate":
            i = block_end
            continue

        branch_insn = next_insn(lines, and_insn + 1)
        if branch_insn is None:
            break

        and_line = None
        safe_between = True
        for j in range(and_insn + 1, branch_insn):
            c = code(lines[j])
            if c == "and $40":
                and_line = j
                continue
            if not c:
                continue
            # These are the only expected instructions around the AND result;
            # all preserve GB flags and publish canonical 6502 state.
            if c in {
                "ldh a, [nes_a]",
                "ldh [nes_a], a",
                "ldh [nes_z_shadow], a",
                "ldh [nes_n_shadow], a",
            }:
                continue
            safe_between = False
            break
        if and_line is None or not safe_between:
            i = branch_insn
            continue

        # Require an immediately following BEQ/BNE-style host-Z branch and no
        # flag-changing reload/retest in front of it. Earlier peepholes normally
        # remove those already; this guard prevents us from depending on Z when
        # a future emitter shape changes.
        next_after_branch = next_insn(lines, branch_insn + 1)
        branch_end = next_after_branch if next_after_branch is not None else len(lines)
        saw_cond = False
        branch_safe = True
        for j in range(branch_insn + 1, branch_end):
            c = code(lines[j])
            if not c:
                continue
            if c in {"ldh a, [nes_z_shadow]", "and a", "bit 7, a"}:
                branch_safe = False
                break
            if COND_JR_RE.fullmatch(c):
                saw_cond = True
                break
            # Generated comments are stripped by code(); direct JP/LD before a
            # condition would indicate an unexpected control-flow shape.
            if c.startswith(("jp ", "ld ", "ldh ", "call ")):
                branch_safe = False
                break
        if not branch_safe or not saw_cond:
            i = branch_insn
            continue

        ind = lines[and_line][: len(lines[and_line]) - len(lines[and_line].lstrip())]
        lines[and_line] = f"{ind}; fused sprite-0 result: A and GB Z already equal AND #$40\n"
        fused += 1
        i = branch_insn

    return fused


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("asm", type=Path)
    args = p.parse_args()

    lines = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    count = fuse(lines)
    args.asm.write_text("".join(lines), encoding="utf-8")
    print(f"sprite0-branch: fused {count} specialized poll results into BEQ/BNE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
