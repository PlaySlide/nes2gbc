#!/usr/bin/env python3
"""Cache hot direct NES zero-page bytes in spare host registers per block.

This pass runs after the proven block-local X/Y cache. It only uses whichever of
LR35902 B/C is still completely unused by the translated block, so existing X/Y
caches keep priority. It may use both B and C when both remain free and two
independent candidates are profitable.

Only direct host addresses $C000-$C0FF are candidates. A block is rejected if it
contains an ordinary helper CALL or any indirect host-memory access through
HL/DE/BC, because such an access could alias a cached zero-page byte. Canonical
NES RAM remains authoritative: stores still write WRAM and then refresh the host
cache. Both GBC interrupt handlers preserve BC, and translated NES NMI delivery
occurs at block-entry poll points, so an NMI return re-enters with no stale
cross-block cache state.

Cache initialization is lazy:
* if the first access is a load, keep that canonical absolute load and copy A
  into the cache register; later reloads use B/C;
* if the first access is a store, the canonical store also initializes the
  cache, so no seed read is needed.

LR35902 M-cycle estimate, independently per cached byte:
    direct absolute load:  ld a,[$C0xx] = 4
    cached load:           ld a,b/c     = 1
    first-load seed copy:  ld r,a       = 1
    store refresh:         extra ld r,a = 1
If the first access is a load, estimated saving is 3*loads - 4 - stores.
If the first access is a store, estimated saving is 3*loads - stores.
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
    for line in body:
        if INDIRECT_MEM_RE.search(code(line)):
            return True
    return False

def zp_stats(body: list[str]) -> dict[int, tuple[int, int, bool, str | None]]:
    loads: dict[int, int] = {}
    stores: dict[int, int] = {}
    bad: set[int] = set()
    first: dict[int, str] = {}
    for line in body:
        c = code(line)
        lm = ZP_LOAD_RE.fullmatch(c)
        if lm:
            addr = int(lm.group(1), 16)
            first.setdefault(addr, "load")
            loads[addr] = loads.get(addr, 0) + 1
            continue
        sm = ZP_STORE_RE.fullmatch(c)
        if sm:
            addr = int(sm.group(1), 16)
            first.setdefault(addr, "store")
            if sm.group(2).lower() == "a":
                stores[addr] = stores.get(addr, 0) + 1
            else:
                bad.add(addr)
    keys = set(loads) | set(stores) | bad
    return {addr: (loads.get(addr, 0), stores.get(addr, 0), addr in bad, first.get(addr)) for addr in keys}

def saving(loads: int, stores: int, first: str | None) -> int:
    if first == "load":
        return 3 * loads - 4 - stores
    if first == "store":
        return 3 * loads - stores
    return -1

def optimize(lines: list[str]) -> tuple[int, int, int, int, int, int]:
    cached_blocks = cached_bytes = replaced_loads = mirrored_stores = 0
    load_seeds = store_seeds = 0
    for block in reversed(blocks(lines)):
        body = lines[block.start + 1 : block.end]
        if has_unsafe_call(body) or has_possible_alias(body):
            continue
        free_regs = [r for r in ("b", "c") if not uses_reg(body, r)]
        if not free_regs:
            continue
        candidates: list[tuple[int, int, int, int, str]] = []
        for addr, (loads, stores, bad_store, first) in zp_stats(body).items():
            if bad_store or loads == 0 or first is None:
                continue
            gain = saving(loads, stores, first)
            if gain > 0:
                candidates.append((gain, addr, loads, stores, first))
        if not candidates:
            continue
        candidates.sort(reverse=True)
        chosen = candidates[: len(free_regs)]
        assignment = {addr: reg for (_gain, addr, _loads, _stores, _first), reg in zip(chosen, free_regs)}
        initialized: set[int] = set()
        i = block.start + 1
        while i < block.end:
            c = code(lines[i])
            lm = ZP_LOAD_RE.fullmatch(c)
            if lm:
                addr = int(lm.group(1), 16)
                reg = assignment.get(addr)
                if reg is not None:
                    ind = indent_of(lines[i])
                    if addr in initialized:
                        lines[i] = f"{ind}ld a, {reg} ; cached NES ZP ${addr:02X}\n"
                        replaced_loads += 1
                    else:
                        lines[i] = lines[i] + f"{ind}ld {reg}, a ; seed NES ZP ${addr:02X} cache\n"
                        initialized.add(addr)
                        load_seeds += 1
                    i += 1
                    continue
            sm = ZP_STORE_RE.fullmatch(c)
            if sm and sm.group(2).lower() == "a":
                addr = int(sm.group(1), 16)
                reg = assignment.get(addr)
                if reg is not None:
                    ind = indent_of(lines[i])
                    was_initialized = addr in initialized
                    lines[i] = lines[i] + f"{ind}ld {reg}, a ; refresh NES ZP ${addr:02X} cache\n"
                    mirrored_stores += 1
                    initialized.add(addr)
                    if not was_initialized:
                        store_seeds += 1
            i += 1
        cached_blocks += 1
        cached_bytes += len(assignment)
    return cached_blocks, cached_bytes, replaced_loads, mirrored_stores, load_seeds, store_seeds

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("asm", type=Path)
    args = p.parse_args()
    lines = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    blocks_n, bytes_n, loads_n, stores_n, load_seeds, store_seeds = optimize(lines)
    args.asm.write_text("".join(lines), encoding="utf-8")
    print(
        f"zp-cache: cached {bytes_n} hot ZP bytes across {blocks_n} blocks, "
        f"replaced {loads_n} absolute loads, mirrored {stores_n} stores; "
        f"seeded {load_seeds} on first load / {store_seeds} from prior store"
    )
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
