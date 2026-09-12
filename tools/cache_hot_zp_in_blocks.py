#!/usr/bin/env python3
"""Cache hot direct NES zero-page bytes in spare host registers per block.

This pass runs after the proven block-local X/Y cache. It only uses whichever of
LR35902 B/C is still completely unused by the translated block, so existing X/Y
caches keep priority. The first version cached only one ZP byte per block; this
version may use both B and C when both remain free and two independent candidates
are profitable.

Only direct host addresses $C000-$C0FF are candidates. A block is rejected if it
contains an ordinary helper CALL or any indirect host-memory access through
HL/DE/BC, because such an access could alias a cached zero-page byte. Canonical
NES RAM remains authoritative: stores still write WRAM and then refresh the host
cache. Both GBC interrupt handlers preserve BC, and translated NES NMI delivery
occurs at block-entry poll points, so an NMI return re-enters and reseeds every
cache.

LR35902 M-cycle estimate, independently per cached byte:
    direct absolute load:  ld a,[$C0xx] = 4
    cached load:           ld a,b/c     = 1
    one-time seed:         absolute load + ld r,a = 5
    store refresh:         extra ld r,a = 1
A candidate is used only when 3*loads - 5 - stores is positive.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path


BLOCK_RE = re.compile(r"^nes_([0-9A-Fa-f]{4}):$")
ZP_LOAD_RE = re.compile(r"ld a, \[\$C0([0-9A-Fa-f]{2})\]$")
ZP_STORE_RE = re.compile(r"ld \[\$C0([0-9A-Fa-f]{2})\], ([a-z0-9$]+)$", re.IGNORECASE)
INDIRECT_MEM_RE = re.compile(r"\[(?:hl|de|bc|hli|hld)\]", re.IGNORECASE)


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


def has_possible_alias(body: list[str]) -> bool:
    """Reject any indirect host-memory access that could touch $C0xx."""
    for line in body:
        c = code(line)
        if INDIRECT_MEM_RE.search(c):
            return True
    return False


def zp_counts(body: list[str]) -> dict[int, tuple[int, int, bool]]:
    """addr -> (loads, stores-from-A, has-unsupported-store)."""
    loads: dict[int, int] = {}
    stores: dict[int, int] = {}
    bad: set[int] = set()

    for line in body:
        c = code(line)
        lm = ZP_LOAD_RE.fullmatch(c)
        if lm:
            addr = int(lm.group(1), 16)
            loads[addr] = loads.get(addr, 0) + 1
            continue
        sm = ZP_STORE_RE.fullmatch(c)
        if sm:
            addr = int(sm.group(1), 16)
            src = sm.group(2).lower()
            if src == "a":
                stores[addr] = stores.get(addr, 0) + 1
            else:
                bad.add(addr)

    keys = set(loads) | set(stores) | bad
    return {
        addr: (loads.get(addr, 0), stores.get(addr, 0), addr in bad)
        for addr in keys
    }


def saving(loads: int, stores: int) -> int:
    return 3 * loads - 5 - stores


def optimize(lines: list[str]) -> tuple[int, int, int, int]:
    cached_blocks = 0
    cached_bytes = 0
    replaced_loads = 0
    mirrored_stores = 0

    # Bottom-up insertion keeps recorded block indexes valid.
    for block in reversed(blocks(lines)):
        body = lines[block.start + 1 : block.end]
        if has_unsafe_call(body) or has_possible_alias(body):
            continue

        free_regs = [r for r in ("b", "c") if not uses_reg(body, r)]
        if not free_regs:
            continue

        candidates: list[tuple[int, int, int, int]] = []
        for addr, (loads, stores, bad_store) in zp_counts(body).items():
            if bad_store or loads == 0:
                continue
            gain = saving(loads, stores)
            if gain > 0:
                candidates.append((gain, addr, loads, stores))
        if not candidates:
            continue

        # Each cached byte is independent. Use at most the number of genuinely
        # spare B/C registers, ordered by estimated cycle saving.
        candidates.sort(reverse=True)
        chosen = candidates[: len(free_regs)]
        assignment = {
            addr: reg
            for (_gain, addr, _loads, _stores), reg in zip(chosen, free_regs)
        }
        if not assignment:
            continue

        seed: list[str] = []
        for addr, reg in assignment.items():
            host = 0xC000 + addr
            seed.extend(
                [
                    f"    ld a, [${host:04X}]\n",
                    f"    ld {reg}, a ; block-local NES ZP ${addr:02X} cache\n",
                ]
            )
        lines[block.start + 1 : block.start + 1] = seed
        delta = len(seed)
        start = block.start + 1 + delta
        end = block.end + delta

        i = start
        while i < end:
            c = code(lines[i])
            lm = ZP_LOAD_RE.fullmatch(c)
            if lm:
                addr = int(lm.group(1), 16)
                reg = assignment.get(addr)
                if reg is not None:
                    ind = indent_of(lines[i])
                    lines[i] = f"{ind}ld a, {reg} ; cached NES ZP ${addr:02X}\n"
                    replaced_loads += 1
                    i += 1
                    continue

            sm = ZP_STORE_RE.fullmatch(c)
            if sm and sm.group(2).lower() == "a":
                addr = int(sm.group(1), 16)
                reg = assignment.get(addr)
                if reg is not None:
                    ind = indent_of(lines[i])
                    lines[i] = lines[i] + f"{ind}ld {reg}, a ; refresh NES ZP ${addr:02X} cache\n"
                    mirrored_stores += 1
            i += 1

        cached_blocks += 1
        cached_bytes += len(assignment)

    return cached_blocks, cached_bytes, replaced_loads, mirrored_stores


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("asm", type=Path)
    args = p.parse_args()

    lines = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    blocks_n, bytes_n, loads_n, stores_n = optimize(lines)
    args.asm.write_text("".join(lines), encoding="utf-8")
    print(
        f"zp-cache: cached {bytes_n} hot ZP bytes across {blocks_n} blocks, "
        f"replaced {loads_n} absolute loads, mirrored {stores_n} stores"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
