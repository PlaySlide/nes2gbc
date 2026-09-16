#!/usr/bin/env python3
"""Cache canonical 6502 A in a spare LR35902 B/C register within a block.

This pass deliberately runs after the already-validated X/Y and hot-ZP caches,
so it never steals a host register from them. It only uses B/C when that
register is otherwise completely unused in the translated block, and skips
blocks containing ordinary helper calls because helpers do not promise to
preserve BC.

Private ``nes_XXXX_trace`` continuations are intentionally excluded.  The
stateful superblock emitter may carry resident X in B and/or Y in C *through*
such a block even when the block itself never references that host register.
A purely block-local "unused register" scan therefore cannot prove B/C is free
there.  Canonical non-trace blocks start without that hidden live-in contract,
so the local proof remains valid for them.

Canonical nes_a in HRAM remains authoritative. Every store to nes_a is kept
and mirrored into the host register; later loads may use the mirror. The GBC
interrupt handlers preserve BC. The optional profile-trace helper is allowed
because it saves/restores BC explicitly.

Cache initialization is lazy:
* if the first nes_a access is a load, keep that canonical HRAM load and copy A
  into the cache register; later reloads become `ld a,b/c`;
* if the first nes_a access is a store, that canonical store also initializes
  the cache, so no seed load is needed.

LR35902 M-cycle estimate:
    canonical load:        ldh a,[nes_a] = 3
    cached load:           ld a,b/c      = 1
    first-load seed copy:  ld r,a        = 1
    store refresh:         extra ld r,a  = 1
If the first access is a load, estimated saving is 2*loads - 3 - stores.
If the first access is a store, estimated saving is 2*loads - stores.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path


BLOCK_RE = re.compile(r"^nes_([0-9A-Fa-f]{4})(?:(_trace))?:$")


def code(line: str) -> str:
    return line.split(";", 1)[0].strip()


def indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


@dataclass
class Block:
    start: int
    end: int
    addr: int
    is_trace: bool


def blocks(lines: list[str]) -> list[Block]:
    labels: list[tuple[int, int, bool]] = []
    for i, line in enumerate(lines):
        m = BLOCK_RE.fullmatch(code(line))
        if m:
            labels.append((i, int(m.group(1), 16), m.group(2) is not None))

    out: list[Block] = []
    for n, (start, addr, is_trace) in enumerate(labels):
        end = labels[n + 1][0] if n + 1 < len(labels) else len(lines)
        for j in range(start + 1, end):
            if code(lines[j]).startswith("SECTION "):
                end = j
                break
        out.append(Block(start, end, addr, is_trace))
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


def a_stats(body: list[str]) -> tuple[int, int, bool, str | None]:
    loads = 0
    stores = 0
    bad_access = False
    first: str | None = None

    for line in body:
        c = code(line)
        if c in {"ldh a, [nes_a]", "ld a, [nes_a]"}:
            if first is None:
                first = "load"
            loads += 1
        elif c in {"ldh [nes_a], a", "ld [nes_a], a"}:
            if first is None:
                first = "store"
            stores += 1
        elif "[nes_a]" in c and c.startswith(("ldh ", "ld ")):
            # An unfamiliar read/write shape means our simple mirror proof does
            # not cover this block. Leave it alone rather than guessing.
            bad_access = True

    return loads, stores, bad_access, first


def estimated_saving(loads: int, stores: int, first: str | None) -> int:
    if first == "load":
        return 2 * loads - 3 - stores
    if first == "store":
        return 2 * loads - stores
    return -1


def optimize(lines: list[str]) -> tuple[int, int, int, int, int]:
    cached_blocks = 0
    replaced_loads = 0
    mirrored_stores = 0
    load_seeds = 0
    store_seeds = 0

    for block in reversed(blocks(lines)):
        # B/C can be an implicit live-through X/Y contract on private trace
        # continuations.  The absence of a textual B/C use in this one block
        # is therefore not proof that either register is actually spare.
        if block.is_trace:
            continue

        body = lines[block.start + 1 : block.end]
        if has_unsafe_call(body):
            continue

        free_regs = [r for r in ("b", "c") if not uses_reg(body, r)]
        if not free_regs:
            continue

        loads, stores, bad_access, first = a_stats(body)
        if bad_access or loads == 0 or first is None:
            continue
        if estimated_saving(loads, stores, first) <= 0:
            continue

        reg = free_regs[0]
        initialized = False

        i = block.start + 1
        while i < block.end:
            c = code(lines[i])
            if c in {"ldh a, [nes_a]", "ld a, [nes_a]"}:
                ind = indent_of(lines[i])
                if initialized:
                    lines[i] = f"{ind}ld a, {reg} ; cached 6502 A\n"
                    replaced_loads += 1
                else:
                    # Preserve the first canonical load and seed the host cache
                    # from the exact architectural A value now in A.
                    lines[i] = lines[i] + f"{ind}ld {reg}, a ; seed 6502 A cache\n"
                    initialized = True
                    load_seeds += 1
            elif c in {"ldh [nes_a], a", "ld [nes_a], a"}:
                ind = indent_of(lines[i])
                was_initialized = initialized
                lines[i] = lines[i] + f"{ind}ld {reg}, a ; refresh 6502 A cache\n"
                mirrored_stores += 1
                initialized = True
                if not was_initialized:
                    store_seeds += 1
            i += 1

        cached_blocks += 1

    return cached_blocks, replaced_loads, mirrored_stores, load_seeds, store_seeds


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("asm", type=Path)
    args = p.parse_args()

    lines = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    blocks_n, loads_n, stores_n, load_seeds, store_seeds = optimize(lines)
    args.asm.write_text("".join(lines), encoding="utf-8")
    print(
        f"a-cache: cached A in {blocks_n} blocks, replaced {loads_n} HRAM loads, "
        f"mirrored {stores_n} stores; seeded {load_seeds} on first load / "
        f"{store_seeds} from prior store"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
