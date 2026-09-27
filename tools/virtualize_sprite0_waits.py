#!/usr/bin/env python3
"""Fast-forward exact $2002/AND #$40 self-loop waits inside translated NMI.

The GBC renderer already reproduces the visible split with STAT/LYC. A translated
NES NMI can span multiple host frames, so tying sprite-0 polling to live host LY
can make an old NES busy-wait stall on an unrelated host raster phase. For the
strict idiom

    LDA $2002
    AND #$40
    BNE/BEQ <same LDA>

preserve the normal physical-raster implementation unless nes_nmi_active != 0.
Inside translated NMI, synthesize the condition that exits the loop immediately:
BNE self-loop gets a clear hit; BEQ self-loop gets a set hit. Preserve PPUSTATUS
read side effects (clear VBlank, reset $2005/$2006 latch), canonical NES A/Z/N,
and leave all other PPUSTATUS reads untouched.

This pass runs after specialize_sprite0_poll.py. Therefore it recognizes both the
original inline-PPUSTATUS marker and the specialized marker that replaces it. It
also treats nes_XXXX_trace labels as real physical block entries: NMI-private
superblocks can put the hot wait body under the trace label while the self-loop
branches through the canonical nes_XXXX adapter.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path

INSN_RE = re.compile(r"; \$([0-9A-Fa-f]{4}): \$([0-9A-Fa-f]{2}) (\w+) (\w+)")
BLOCK_RE = re.compile(r"^nes_([0-9A-Fa-f]{4})(?:_trace)?:$")
COND_SELF_RE = re.compile(r"^(?:jp|jr) (z|nz), nes_([0-9A-Fa-f]{4})$")
MARKERS = (
    "inline exact PPUSTATUS ($2002) read",
    "specialized $2002 -> AND #$40 sprite-0 poll",
)


def code(line: str) -> str:
    return line.split(";", 1)[0].strip()


def insn(line: str):
    m = INSN_RE.search(line.strip())
    if not m:
        return None
    return int(m.group(1), 16), int(m.group(2), 16), m.group(3), m.group(4)


def indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def is_marker(line: str) -> bool:
    return any(marker in line for marker in MARKERS)


@dataclass
class Candidate:
    marker_i: int
    branch_jump_i: int
    addr: int
    wait_for_set: bool


def block_end(lines: list[str], start: int) -> int:
    for i in range(start + 1, len(lines)):
        c = code(lines[i])
        if BLOCK_RE.fullmatch(c) or c.startswith("SECTION "):
            return i
    return len(lines)


def find_candidates(lines: list[str]) -> list[Candidate]:
    out: list[Candidate] = []
    physical_addr: int | None = None
    physical_start = 0
    physical_end = len(lines)

    for i, line in enumerate(lines):
        bm = BLOCK_RE.fullmatch(code(line))
        if bm:
            physical_addr = int(bm.group(1), 16)
            physical_start = i
            physical_end = block_end(lines, i)
            continue
        if not is_marker(line) or physical_addr is None or i >= physical_end:
            continue

        # Prove the exact source idiom by source PCs rather than depending on
        # where batched comments happen to land relative to the marker. The hot
        # body may live at nes_XXXX_trace while the branch targets nes_XXXX.
        by_pc: dict[int, tuple[int, tuple[int, int, str, str]]] = {}
        for j in range(physical_start + 1, physical_end):
            x = insn(lines[j])
            if x is not None:
                by_pc.setdefault(x[0], (j, x))

        lda_info = by_pc.get(physical_addr)
        and_info = by_pc.get((physical_addr + 3) & 0xFFFF)
        branch_info = by_pc.get((physical_addr + 5) & 0xFFFF)
        if lda_info is None or and_info is None or branch_info is None:
            continue
        lda_i, lda = lda_info
        and_i, and_src = and_info
        branch_i, branch = branch_info
        if not (
            lda[2:] == ("Lda", "Absolute")
            and and_src[2:] == ("And", "Immediate")
            and branch[2] in {"Beq", "Bne"}
            and branch[3] == "Relative"
            and lda_i <= i < branch_i
        ):
            continue

        # Prove #$40 from emitted code. The sprite0 branch fuser may replace the
        # literal AND with a comment while retaining the same exact semantics.
        mask_proven = any(code(lines[j]) == "and $40" for j in range(and_i + 1, branch_i))
        if not mask_proven:
            mask_proven = any(
                "fused sprite-0 result: A and GB Z already equal AND #$40" in lines[j]
                for j in range(and_i + 1, branch_i)
            )
        if not mask_proven:
            continue

        expected_cond = "z" if branch[2] == "Beq" else "nz"
        jump_i = None
        for j in range(branch_i + 1, physical_end):
            m = COND_SELF_RE.fullmatch(code(lines[j]))
            if not m:
                continue
            if m.group(1) == expected_cond and int(m.group(2), 16) == physical_addr:
                jump_i = j
                break
        if jump_i is None:
            continue

        out.append(Candidate(i, jump_i, physical_addr, branch[2] == "Beq"))

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
        fast.extend(
            [
                f"{ind}ld [nes_ppu_status], a\n",
                f"{ind}xor a\n",
                f"{ind}ld [nes_ppu_latch], a\n",
            ]
        )
        if c.wait_for_set:
            fast.append(f"{ind}ld a, $40\n")
        fast.extend(
            [
                f"{ind}ldh [nes_a], a\n",
                f"{ind}ldh [nes_z_shadow], a\n",
                f"{ind}ldh [nes_n_shadow], a\n",
                f"{ind}jp {done}\n",
                f"{physical}:\n",
            ]
        )

        # Put the fast-path destination immediately after the original self
        # branch. Physical execution still loops exactly as before.
        lines.insert(c.branch_jump_i + 1, f"{done}:\n")
        lines[c.marker_i + 1 : c.marker_i + 1] = fast

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
