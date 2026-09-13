#!/usr/bin/env python3
"""Cache hot 6502 X/Y values in unused LR35902 B/C within a basic block.

This is intentionally local and conservative. We only use B/C because both
host interrupt handlers preserve BC; D/E are not safe across STAT. Blocks that
contain any ordinary CALL are skipped because runtime helpers do not promise to
preserve BC. The optional profile-trace helper is allowed: it explicitly saves
and restores BC.

For an eligible block, a cache is only worthwhile when the estimated cycle
saving is positive. Cache initialization is lazy:

* if the first X/Y access is a load, keep that first HRAM load and copy A into
  the cache register; later reloads become `ld a,b/c`;
* if the first X/Y access is a store, the canonical store also initializes the
  cache, so no HRAM seed load is needed at all.

This avoids the old redundant block-entry seed followed by an immediate cached
reload at the first real use, and admits some blocks where a write establishes
the cached value before the first read.

Canonical HRAM state remains authoritative. Every write to nes_x/nes_y still
happens normally and is mirrored into the chosen host register afterward.

Aligned same-bank PRG mirrors have another exact composition opportunity. Their
low address byte is guaranteed to be $00, so a cached X/Y reload immediately
followed by `ld l,a` can become `ld l,b/c` directly. This removes the otherwise
redundant move through A without affecting host flags.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path


BLOCK_RE = re.compile(r"^nes_([0-9A-Fa-f]{4}):$")
CACHED_INDEX_RE = re.compile(r"ld a, ([bc])\s*;\s*cached nes_([xy])$", re.IGNORECASE)


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


def access_kind(line: str, state: str) -> str | None:
    c = code(line)
    if c in {f"ldh a, [{state}]", f"ld a, [{state}]"}:
        return "load"
    if c in {f"ldh [{state}], a", f"ld [{state}], a"}:
        return "store"
    return None


def state_stats(body: list[str], state: str) -> tuple[int, int, str | None]:
    loads = 0
    stores = 0
    first: str | None = None
    for line in body:
        kind = access_kind(line, state)
        if kind is None:
            continue
        if first is None:
            first = kind
        if kind == "load":
            loads += 1
        else:
            stores += 1
    return loads, stores, first


def estimated_saving(loads: int, stores: int, first: str | None) -> int:
    """Estimated M-cycle saving relative to canonical HRAM reloads.

    HRAM load costs 3 M-cycles; cached `ld a,r` costs 1; cache refresh `ld r,a`
    costs 1. If the first access is a load, that load remains and pays one extra
    cache-copy cycle, so saving is 2*loads - 3 - stores. If the first access is
    a store, that store initializes the cache for free apart from its required
    mirror, so saving is 2*loads - stores.
    """
    if first == "load":
        return 2 * loads - 3 - stores
    if first == "store":
        return 2 * loads - stores
    return -1


def fold_aligned_prg_cached_indexes(lines: list[str]) -> int:
    """Feed cached X/Y directly into L for validated 256-byte PRG mirrors."""
    folded = 0
    for i in range(len(lines) - 2):
        m = CACHED_INDEX_RE.fullmatch(lines[i].strip())
        if not m:
            continue
        reg = m.group(1).lower()
        # The marker is emitted only by mirror_indexed_prg_tables.py after the
        # table section has been ALIGN[8]-constrained. Require the following
        # direct table load too so A has no observable use as the index value.
        if "256-byte-aligned table: low byte is index" not in lines[i + 1]:
            continue
        if code(lines[i + 1]) != "ld l, a" or code(lines[i + 2]) != "ld a, [hl]":
            continue
        indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
        lines[i] = f"{indent}; cached index moved directly into L\n"
        lines[i + 1] = f"{indent}ld l, {reg} ; cached X/Y + 256-byte-aligned PRG table\n"
        folded += 1
    return folded


def optimize(lines: list[str]) -> tuple[int, int, int, int, int, int]:
    bs = blocks(lines)
    cached_x_blocks = 0
    cached_y_blocks = 0
    replaced_loads = 0
    load_seeds = 0
    store_seeds = 0

    # Rewrite bottom-up even though we no longer insert list elements; keeping
    # this order makes the pass robust if a later refinement adds local inserts.
    for block in reversed(bs):
        body = lines[block.start + 1 : block.end]
        if has_unsafe_call(body):
            continue

        free_regs = [r for r in ("b", "c") if not uses_reg(body, r)]
        if not free_regs:
            continue

        candidates: list[tuple[int, str, int, int, str]] = []
        for state in ("nes_x", "nes_y"):
            loads, stores, first = state_stats(body, state)
            saving = estimated_saving(loads, stores, first)
            if saving > 0 and first is not None:
                candidates.append((saving, state, loads, stores, first))

        if not candidates:
            continue
        candidates.sort(reverse=True)

        assignment: dict[str, str] = {}
        for (_saving, state, _loads, _stores, _first), reg in zip(candidates, free_regs):
            assignment[state] = reg
        if not assignment:
            continue

        initialized: set[str] = set()
        i = block.start + 1
        while i < block.end:
            c = code(lines[i])
            for state, reg in assignment.items():
                load_forms = {f"ldh a, [{state}]", f"ld a, [{state}]"}
                store_forms = {f"ldh [{state}], a", f"ld [{state}], a"}

                if c in load_forms:
                    indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
                    if state in initialized:
                        lines[i] = f"{indent}ld a, {reg} ; cached {state}\n"
                        replaced_loads += 1
                    else:
                        # Keep the first canonical HRAM load, then seed the cache
                        # from the exact value now in A. LD does not affect flags.
                        lines[i] = lines[i] + f"{indent}ld {reg}, a ; seed {state} cache\n"
                        initialized.add(state)
                        load_seeds += 1
                    break

                if c in store_forms:
                    indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
                    was_initialized = state in initialized
                    # Keep the canonical write exactly as-is, then mirror the
                    # new value into the host cache without touching flags.
                    lines[i] = lines[i] + f"{indent}ld {reg}, a ; refresh {state} cache\n"
                    initialized.add(state)
                    if not was_initialized:
                        store_seeds += 1
                    break
            i += 1

        if "nes_x" in assignment:
            cached_x_blocks += 1
        if "nes_y" in assignment:
            cached_y_blocks += 1

    prg_direct = fold_aligned_prg_cached_indexes(lines)
    return cached_x_blocks, cached_y_blocks, replaced_loads, load_seeds, store_seeds, prg_direct


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("asm", type=Path)
    args = p.parse_args()

    lines = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    x_blocks, y_blocks, loads, load_seeds, store_seeds, prg_direct = optimize(lines)
    args.asm.write_text("".join(lines), encoding="utf-8")
    print(
        f"xy-cache: cached X in {x_blocks} blocks, Y in {y_blocks} blocks, "
        f"replaced {loads} HRAM index reloads; seeded {load_seeds} on first load / "
        f"{store_seeds} from prior store; fed {prg_direct} cached index(es) directly "
        f"into aligned PRG tables"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
