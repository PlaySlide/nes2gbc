#!/usr/bin/env python3
"""Cache canonical 6502 A in a spare LR35902 B/C register within a block.

This pass deliberately runs after the already-validated X/Y and hot-ZP caches,
so it never steals a host register from them.  It only uses B/C when that
register is otherwise completely unused in the translated block, and skips
blocks containing ordinary helper calls because helpers do not promise to
preserve BC.

Canonical nes_a in HRAM remains authoritative.  Every store to nes_a is kept
and mirrored into the host register; loads may then use the mirror.  The GBC
interrupt handlers preserve BC.  The optional profile-trace helper is allowed
because it saves/restores BC explicitly.

LR35902 M-cycle estimate:
    canonical load:  ldh a,[nes_a] = 3
    cached load:     ld a,b/c      = 1
    one-time seed:   ldh + ld      = 4
    store refresh:   extra ld      = 1
A cache is installed only when 2*loads - 4 - stores is positive.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path


BLOCK_RE = re.compile(r"^nes_([0-9A-Fa-f]{4}):$")


def code(line: str) -> str:
    return line.split(";", 1)[0].strip()


def indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


@dataclass
class Block:
    start: int
    end: int
    addr: int


def blocks(lines: list[str]) -> list[Block]:
    labels: list[tuple[int, int]] = []
    for i, line in enumerate(lines):
        m = BLOCK_RE.fullmatch(code(line))
        if m:
            labels.append((i, int(m.group(1), 16)))

    out: list[Block] = []
    for n, (start, addr) in enumerate(labels):
        end = labels[n + 1][0] if n + 1 < len(labels) else len(lines)
        for j in range(start + 1, end):
            if code(lines[j]).startswith("SECTION "):
                end = j
                break
        out.append(Block(start, end, addr))
    return out


def uses_reg(body: list[str], reg: str) -> bool:
    r = reg.lower()
    single = re.compile(rf"\b{r}\b")
    pair = re.compile(r"\bbc\b")
    for line in body:
        c = code(line).lower()
        if not c or c.endswith(":"):
            continue
        if single.search(c) or pair.search(c):
            return True
    return False


def has_unsafe_call(body: list[str]) -> bool:
    for line in body:
        c = code(line)
        if not c.startswith("call "):
            continue
        if c == "call nes_profile_trace_pc":
            continue
        return True
    return False


def a_counts(body: list[str]) -> tuple[int, int, bool]:
    loads = 0
    stores = 0
    bad_store = False
    for line in body:
        c = code(line)
        if c in {"ldh a, [nes_a]", "ld a, [nes_a]"}:
            loads += 1
        elif c in {"ldh [nes_a], a", "ld [nes_a], a"}:
            stores += 1
        elif "[nes_a]" in c and c.startswith(("ldh ", "ld ")):
            # An unfamiliar write/read shape means our simple mirror proof does
            # not cover this block. Leave it alone rather than guessing.
            bad_store = True
    return loads, stores, bad_store


def optimize(lines: list[str]) -> tuple[int, int, int]:
    cached_blocks = 0
    replaced_loads = 0
    mirrored_stores = 0

    # Bottom-up insertion keeps recorded block indexes valid.
    for block in reversed(blocks(lines)):
        body = lines[block.start + 1 : block.end]
        if has_unsafe_call(body):
            continue

        free_regs = [r for r in ("b", "c") if not uses_reg(body, r)]
        if not free_regs:
            continue

        loads, stores, bad_store = a_counts(body)
        if bad_store or loads == 0:
            continue
        if 2 * loads - 4 - stores <= 0:
            continue

        reg = free_regs[0]
        seed = [
            "    ldh a, [nes_a]\n",
            f"    ld {reg}, a ; block-local 6502 A cache\n",
        ]
        lines[block.start + 1 : block.start + 1] = seed
        delta = len(seed)
        start = block.start + 1 + delta
        end = block.end + delta

        i = start
        while i < end:
            c = code(lines[i])
            if c in {"ldh a, [nes_a]", "ld a, [nes_a]"}:
                ind = indent_of(lines[i])
                lines[i] = f"{ind}ld a, {reg} ; cached 6502 A\n"
                replaced_loads += 1
            elif c in {"ldh [nes_a], a", "ld [nes_a], a"}:
                ind = indent_of(lines[i])
                lines[i] = lines[i] + f"{ind}ld {reg}, a ; refresh 6502 A cache\n"
                mirrored_stores += 1
            i += 1

        cached_blocks += 1

    return cached_blocks, replaced_loads, mirrored_stores


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("asm", type=Path)
    args = p.parse_args()

    lines = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    blocks_n, loads_n, stores_n = optimize(lines)
    args.asm.write_text("".join(lines), encoding="utf-8")
    print(
        f"a-cache: cached A in {blocks_n} blocks, replaced {loads_n} HRAM loads, "
        f"mirrored {stores_n} stores"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
