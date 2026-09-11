#!/usr/bin/env python3
"""Cache hot 6502 X/Y values in unused LR35902 B/C within a basic block.

This is intentionally local and conservative.  The Rust emitter treats every
translated basic-block entry as having no live host accumulator state, so using
A to seed a cache at the block label is harmless.  We only use B/C because both
host interrupt handlers preserve BC; D/E are not safe across STAT.  Blocks that
contain any ordinary CALL are skipped because runtime helpers do not promise to
preserve BC.  The optional profile-trace helper is allowed: it explicitly saves
and restores BC.

For an eligible block, a cache is only worthwhile when the estimated cycle
saving is positive:
    baseline X/Y reload: ldh a,[nes_x/y]          = 3 M-cycles
    cached reload:       ld a,b/c                 = 1 M-cycle
    one-time seed:       ldh a,[state] + ld r,a   = 4 M-cycles
    state update:        extra ld r,a             = 1 M-cycle

Canonical HRAM state remains authoritative.  Every write to nes_x/nes_y still
happens normally and is mirrored into the chosen host register afterward.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path


BLOCK_RE = re.compile(r"^nes_([0-9A-Fa-f]{4}):$")


def code(line: str) -> str:
    return line.split(";", 1)[0].strip()


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
        # Do not let the last canonical block absorb later dispatch/mirror data.
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
        if single.search(c):
            return True
        if pair.search(c):
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


def state_counts(body: list[str], state: str) -> tuple[int, int]:
    loads = 0
    stores = 0
    for line in body:
        c = code(line)
        if c in {f"ldh a, [{state}]", f"ld a, [{state}]"}:
            loads += 1
        elif c in {f"ldh [{state}], a", f"ld [{state}], a"}:
            stores += 1
    return loads, stores


def estimated_saving(loads: int, stores: int) -> int:
    # M-cycle estimate described in the module docstring.
    return 2 * loads - 4 - stores


def optimize(lines: list[str]) -> tuple[int, int, int]:
    bs = blocks(lines)
    # Rewrite bottom-up so insertion does not invalidate earlier block indexes.
    cached_x_blocks = 0
    cached_y_blocks = 0
    replaced_loads = 0

    for block in reversed(bs):
        body = lines[block.start + 1 : block.end]
        if has_unsafe_call(body):
            continue

        free_regs = [r for r in ("b", "c") if not uses_reg(body, r)]
        if not free_regs:
            continue

        candidates: list[tuple[int, str, int, int]] = []
        for state in ("nes_x", "nes_y"):
            loads, stores = state_counts(body, state)
            saving = estimated_saving(loads, stores)
            if saving > 0:
                candidates.append((saving, state, loads, stores))

        if not candidates:
            continue
        candidates.sort(reverse=True)

        assignment: dict[str, str] = {}
        for (_saving, state, _loads, _stores), reg in zip(candidates, free_regs):
            assignment[state] = reg
        if not assignment:
            continue

        # Seed caches immediately after the canonical block label.  LD/LDH do
        # not affect flags.  The emitter never carries A as an architectural
        # assumption across block entries; canonical nes_a remains in HRAM.
        seed: list[str] = []
        for state, reg in assignment.items():
            seed.append(f"    ldh a, [{state}]\n")
            seed.append(f"    ld {reg}, a ; block-local {state} cache\n")
        lines[block.start + 1 : block.start + 1] = seed

        # Account for the insertion while rewriting the original body range.
        delta = len(seed)
        start = block.start + 1 + delta
        end = block.end + delta
        i = start
        while i < end:
            c = code(lines[i])
            changed = False
            for state, reg in assignment.items():
                if c in {f"ldh a, [{state}]", f"ld a, [{state}]"}:
                    lines[i] = f"    ld a, {reg} ; cached {state}\n"
                    replaced_loads += 1
                    changed = True
                    break
                if c in {f"ldh [{state}], a", f"ld [{state}], a"}:
                    # Keep the canonical write exactly as-is, then mirror the
                    # new value into the host cache without touching flags.
                    indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
                    lines[i] = lines[i] + f"{indent}ld {reg}, a ; refresh {state} cache\n"
                    changed = True
                    break
            i += 1

        if "nes_x" in assignment:
            cached_x_blocks += 1
        if "nes_y" in assignment:
            cached_y_blocks += 1

    return cached_x_blocks, cached_y_blocks, replaced_loads


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("asm", type=Path)
    args = p.parse_args()

    lines = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    x_blocks, y_blocks, loads = optimize(lines)
    args.asm.write_text("".join(lines), encoding="utf-8")
    print(
        f"xy-cache: cached X in {x_blocks} blocks, Y in {y_blocks} blocks, "
        f"replaced {loads} HRAM index reloads"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
