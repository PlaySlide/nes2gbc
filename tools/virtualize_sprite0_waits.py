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
import os
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
    nmi_only: bool = True


COUNTERS = {"Inx", "Iny", "Dex", "Dey"}


def mainline_enabled() -> bool:
    return os.environ.get("NES2GBC_SPRITE0_VIRTUAL_MAIN", "1") != "0"


def counter_heads(lines: list[str]) -> set[int]:
    """Blocks T that are exactly `INX|INY|DEX|DEY / BEQ|BNE <exit>` and fall
    through into T+3: the timeout head of a counted sprite-0 wait loop."""
    out: set[int] = set()
    cur = None
    insns: list = []

    def close():
        if cur is None or len(insns) != 2:
            return
        a, b = insns
        if a[0] == cur and a[2] in COUNTERS and a[3] == "Implied" \
                and b[0] == (cur + 1) & 0xFFFF and b[2] in {"Beq", "Bne"} and b[3] == "Relative":
            out.add(cur)

    for line in lines:
        c = code(line)
        bm = BLOCK_RE.fullmatch(c)
        if bm or c.startswith("SECTION "):
            close()
            cur = int(bm.group(1), 16) if bm else None
            insns = []
            continue
        x = insn(line)
        if x is not None and (not insns or insns[-1][0] != x[0]):
            insns.append(x)
    close()
    return out


def block_end(lines: list[str], start: int) -> int:
    for i in range(start + 1, len(lines)):
        c = code(lines[i])
        if BLOCK_RE.fullmatch(c) or c.startswith("SECTION "):
            return i
    return len(lines)


def poll_blocks(lines: list[str]) -> set[int]:
    """Blocks nes_P that start with a marked $2002 read at P followed by
    AND #imm at P+3 and BEQ/BNE at P+5 (any target)."""
    out: set[int] = set()
    cur = None
    seen: dict[int, tuple] = {}
    marked = False

    def close():
        if cur is None or not marked:
            return
        a, b, c = seen.get(cur), seen.get((cur + 3) & 0xFFFF), seen.get((cur + 5) & 0xFFFF)
        if a and b and c and a[2:] == ("Lda", "Absolute") and b[2:] == ("And", "Immediate") \
                and c[2] in {"Beq", "Bne"}:
            out.add(cur)

    for line in lines:
        c = code(line)
        bm = BLOCK_RE.fullmatch(c)
        if bm or c.startswith("SECTION "):
            close()
            cur = int(bm.group(1), 16) if bm else None
            seen = {}
            marked = False
            continue
        if is_marker(line):
            marked = True
        x = insn(line)
        if x is not None:
            seen.setdefault(x[0], x)
    close()
    return out


def find_candidates(lines: list[str]) -> list[Candidate]:
    out: list[Candidate] = []
    heads = counter_heads(lines) if mainline_enabled() else set()
    polls = poll_blocks(lines) if heads else set()

    def chain_ok(t: int, p: int) -> bool:
        # head T falls through T+3, T+10, ... (each a 7-byte $2002 poll
        # block) down to P
        q = (t + 3) & 0xFFFF
        for _ in range(4):
            if q == p:
                return True
            if q not in polls:
                return False
            q = (q + 7) & 0xFFFF
        return False
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
            at = j
            if m and m.group(1) == expected_cond:
                t = int(m.group(2), 16)
            else:
                # undispatched form: jr <!cond>, :+ / ld hl, $T / jp nes_dispatch_hl / :
                m2 = re.fullmatch(r"jr (z|nz), :\+", code(lines[j]))
                if not (m2 and mainline_enabled() and j + 3 < physical_end):
                    continue
                inv = "nz" if m2.group(1) == "z" else "z"
                m3 = re.fullmatch(r"ld hl, \$([0-9A-Fa-f]{4})", code(lines[j + 1]))
                if inv != expected_cond or not m3 or code(lines[j + 2]) != "jp nes_dispatch_hl" \
                        or code(lines[j + 3]) != ":":
                    continue
                t = int(m3.group(1), 16)
                at = j + 3
            if t == physical_addr or (t in heads and chain_ok(t, physical_addr)):
                jump_i = at
                break
        if jump_i is None:
            continue

        out.append(Candidate(i, jump_i, physical_addr, branch[2] == "Beq",
                             nmi_only=not mainline_enabled()))

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

        virt = f"{tag}_virtual"
        if c.nmi_only:
            fast = [
                f"{ind}; virtualized exact sprite-0 {'set' if c.wait_for_set else 'clear'} wait while translated NMI is active\n",
                f"{ind}ld a, [nes_nmi_active]\n",
                f"{ind}and a\n",
                f"{ind}jr z, {physical}\n",
            ]
        else:
            # Mainline too: the renderer places the split from STAT/LYC, so
            # the wait need not track the live host raster. A set wait still
            # requires a hit to be possible this frame (rendering on, sprite 0
            # on screen); otherwise the physical poll keeps its exact result.
            fast = [f"{ind}; virtualized sprite-0 {'set' if c.wait_for_set else 'clear'} wait (NMI or mainline)\n"]
            if c.wait_for_set:
                fast += [
                    f"{ind}ld a, [nes_nmi_active]\n",
                    f"{ind}and a\n",
                    f"{ind}jr nz, {virt}\n",
                    f"{ind}ld a, [nes_ppumask]\n",
                    f"{ind}and $18\n",
                    f"{ind}cp $18\n",
                    f"{ind}jr nz, {physical}\n",
                    f"{ind}ld a, [nes_oam_ram]\n",
                    f"{ind}cp $EF\n",
                    f"{ind}jr nc, {physical}\n",
                    f"{virt}:\n",
                ]
        fast += [
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
        f"physical fallback preserved (mainline={'on' if mainline_enabled() else 'off'})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
