#!/usr/bin/env python3
"""Fast-forward exact $2002/AND #$40 self-loop waits inside translated NMI.

The GBC renderer already reproduces the visible split with STAT/LYC.  A translated
NES NMI can span multiple host frames, so tying sprite-0 polling to live host LY
can make an old NES busy-wait stall on an unrelated host raster phase.  For the
strict idiom

    LDA $2002
    AND #$40
    BNE/BEQ <same LDA>

preserve the normal physical-raster implementation unless nes_nmi_active != 0.
Inside translated NMI, synthesize the condition that exits the loop immediately:
BNE self-loop gets a clear hit; BEQ self-loop gets a set hit.  Preserve PPUSTATUS
read side effects (clear VBlank, reset $2005/$2006 latch), canonical NES A/Z/N,
and leave all other PPUSTATUS reads untouched.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path

INSN_RE = re.compile(r"; \$([0-9A-Fa-f]{4}): \$([0-9A-Fa-f]{2}) (\w+) (\w+)")
BLOCK_RE = re.compile(r"^nes_([0-9A-Fa-f]{4}):$")
COND_SELF_RE = re.compile(r"^(?:jp|jr) (z|nz), nes_([0-9A-Fa-f]{4})$")


def code(line: str) -> str:
    return line.split(";", 1)[0].strip()


def insn(line: str):
    m = INSN_RE.search(line.strip())
    if not m:
        return None
    return int(m.group(1), 16), int(m.group(2), 16), m.group(3), m.group(4)


def indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


@dataclass
class Candidate:
    marker_i: int
    branch_jump_i: int
    addr: int
    wait_for_set: bool


def find_candidates(lines: list[str]) -> list[Candidate]:
    out: list[Candidate] = []
    block_addr: int | None = None
    block_start = 0

    for i, line in enumerate(lines):
        bm = BLOCK_RE.fullmatch(code(line))
        if bm:
            block_addr = int(bm.group(1), 16)
            block_start = i
            continue
        if "inline exact PPUSTATUS ($2002) read" not in line or block_addr is None:
            continue

        # Because the emitter batches non-control IR, the LDA and AND source
        # comments may both appear immediately before the inlined $2002 body.
        prev = []
        for j in range(i - 1, block_start, -1):
            x = insn(lines[j])
            if x is not None:
                prev.append(x)
                if len(prev) == 2:
                    break
        if len(prev) != 2:
            continue
        and_i, lda_i = prev[0], prev[1]
        if not (lda_i[2:] == ("Lda", "Absolute") and and_i[2:] == ("And", "Immediate")):
            continue
        if lda_i[0] != block_addr or and_i[0] != ((lda_i[0] + 3) & 0xFFFF):
            continue

        # The next source instruction must be BEQ/BNE, immediately after the
        # 3-byte LDA and 2-byte AND.
        branch_comment_i = None
        branch = None
        for j in range(i + 1, len(lines)):
            if BLOCK_RE.fullmatch(code(lines[j])):
                break
            x = insn(lines[j])
            if x is not None:
                branch_comment_i = j
                branch = x
                break
        if branch_comment_i is None or branch is None:
            continue
        if branch[2] not in {"Beq", "Bne"} or branch[3] != "Relative":
            continue
        if branch[0] != ((lda_i[0] + 5) & 0xFFFF):
            continue

        # Prove the immediate mask from emitted code, not source comments.
        if not any(code(lines[j]) == "and $40" for j in range(i + 1, branch_comment_i)):
            continue

        expected_cond = "z" if branch[2] == "Beq" else "nz"
        jump_i = None
        for j in range(branch_comment_i + 1, len(lines)):
            if BLOCK_RE.fullmatch(code(lines[j])) or insn(lines[j]) is not None:
                break
            m = COND_SELF_RE.fullmatch(code(lines[j]))
            if not m:
                continue
            if m.group(1) == expected_cond and int(m.group(2), 16) == block_addr:
                jump_i = j
                break
        if jump_i is None:
            continue

        out.append(Candidate(i, jump_i, block_addr, branch[2] == "Beq"))

    return out


def apply(lines: list[str], candidates: list[Candidate]) -> tuple[int, int]:
    clear_waits = 0
    set_waits = 0

    # Bottom-up keeps all recorded line indices valid.
    for c in sorted(candidates, key=lambda x: x.marker_i, reverse=True):
        ind = indent_of(lines[c.marker_i])
        tag = f"nes_virtual_sprite0_{c.addr:04X}"
        physical = f"{tag}_physical"
        done = f"{tag}_done"

        fast = [
            f"{ind}; virtualized exact sprite-0 {'set' if c.wait_for_set else 'clear'} wait while translated NMI is active\n",
            f"{ind}ld a, [nes_nmi_active]\n",
            f"{ind}and a\n",
            f"{ind}jr z, {physical}\n",
            f"{ind}ld a, [nes_ppu_status]\n",
            f"{ind}and $3F\n",
        ]
        if c.wait_for_set:
            fast.append(f"{ind}or $40\n")
        fast.extend([
            f"{ind}ld [nes_ppu_status], a\n",
            f"{ind}xor a\n",
            f"{ind}ld [nes_ppu_latch], a\n",
        ])
        if c.wait_for_set:
            fast.append(f"{ind}ld a, $40\n")
        fast.extend([
            f"{ind}ldh [nes_a], a\n",
            f"{ind}ldh [nes_z_shadow], a\n",
            f"{ind}ldh [nes_n_shadow], a\n",
            f"{ind}jp {done}\n",
            f"{physical}:\n",
        ])

        # Put the fast-path destination immediately after the original self
        # branch. Physical execution still loops exactly as before.
        lines.insert(c.branch_jump_i + 1, f"{done}:\n")
        lines[c.marker_i + 1:c.marker_i + 1] = fast

        if c.wait_for_set:
            set_waits += 1
        else:
            clear_waits += 1

    return clear_waits, set_waits


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("asm", type=Path)
    args = p.parse_args()

    lines = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    candidates = find_candidates(lines)
    clear_waits, set_waits = apply(lines, candidates)
    args.asm.write_text("".join(lines), encoding="utf-8")
    print(
        "sprite0-wait-virtual: fast-forwarded "
        f"{clear_waits} clear + {set_waits} set exact self-loop wait(s) inside translated NMI; "
        "physical fallback preserved"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
