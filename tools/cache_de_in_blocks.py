#!/usr/bin/env python3
"""Use otherwise-free LR35902 D/E as a second block-local cache tier.

The validated B/C cache passes run first and retain priority. This pass fills
only D/E with profitable remaining values from:
  * canonical 6502 X/Y/A HRAM state; and
  * direct NES zero-page WRAM bytes ($C000-$C0FF).

It skips any block containing a CALL, so runtime helper register conventions are
irrelevant. D/E are safe across enabled hardware interrupts: VBlank saves DE,
and the STAT ISR plus its split/map callees do not touch D/E.

Canonical state remains authoritative. Stores are preserved and mirrored into
the cache. For zero-page values, blocks with any indirect host-memory access are
not considered for ZP caching because that access could alias the cached byte.
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
CACHED_INDEX_RE = re.compile(
    r"ld a, ([de])\s*;\s*DE cache nes_([xy])$", re.IGNORECASE
)
PAGE_BASE_RE = re.compile(r"ld hl, \$[0-9A-Fa-f]{2}00$", re.IGNORECASE)


def code(line: str) -> str:
    return line.split(";", 1)[0].strip()


def indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def next_code_index(lines: list[str], start: int, ceiling: int | None = None) -> int | None:
    end = len(lines) if ceiling is None else min(ceiling, len(lines))
    for i in range(start, end):
        if code(lines[i]):
            return i
    return None


def prev_code_index(lines: list[str], start: int, floor: int = 0) -> int | None:
    for i in range(start, floor - 1, -1):
        if code(lines[i]):
            return i
    return None


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


def uses_de_reg(body: list[str], reg: str) -> bool:
    single = re.compile(rf"\b{reg}\b", re.IGNORECASE)
    pair = re.compile(r"\bde\b", re.IGNORECASE)
    for line in body:
        c = code(line)
        if not c or c.endswith(":"):
            continue
        if single.search(c) or pair.search(c):
            return True
    return False


def has_call(body: list[str]) -> bool:
    return any(code(line).startswith("call ") for line in body)


def state_access(line: str, state: str) -> str | None:
    c = code(line)
    if c in {f"ldh a, [{state}]", f"ld a, [{state}]"}:
        return "load"
    if c in {f"ldh [{state}], a", f"ld [{state}], a"}:
        return "store"
    return None


def state_stats(body: list[str], state: str) -> tuple[int, int, str | None]:
    loads = stores = 0
    first: str | None = None
    for line in body:
        kind = state_access(line, state)
        if kind is None:
            continue
        if first is None:
            first = kind
        if kind == "load":
            loads += 1
        else:
            stores += 1
    return loads, stores, first


def hram_gain(loads: int, stores: int, first: str | None) -> int:
    if first == "load":
        return 2 * loads - 3 - stores
    if first == "store":
        return 2 * loads - stores
    return -1


def zp_stats(body: list[str]) -> dict[int, tuple[int, int, bool, str | None]]:
    loads: dict[int, int] = {}
    stores: dict[int, int] = {}
    bad: set[int] = set()
    first: dict[int, str] = {}

    for line in body:
        c = code(line)
        if m := ZP_LOAD_RE.fullmatch(c):
            addr = int(m.group(1), 16)
            first.setdefault(addr, "load")
            loads[addr] = loads.get(addr, 0) + 1
            continue
        if m := ZP_STORE_RE.fullmatch(c):
            addr = int(m.group(1), 16)
            first.setdefault(addr, "store")
            if m.group(2).lower() == "a":
                stores[addr] = stores.get(addr, 0) + 1
            else:
                bad.add(addr)

    keys = set(loads) | set(stores) | bad
    return {
        addr: (loads.get(addr, 0), stores.get(addr, 0), addr in bad, first.get(addr))
        for addr in keys
    }


def zp_gain(loads: int, stores: int, first: str | None) -> int:
    if first == "load":
        return 3 * loads - 4 - stores
    if first == "store":
        return 3 * loads - stores
    return -1


def fold_direct_cached_indexes(lines: list[str]) -> tuple[int, int, int]:
    prg = page = zp0 = 0
    for i in range(len(lines)):
        m = CACHED_INDEX_RE.fullmatch(lines[i].strip())
        if not m:
            continue
        reg = m.group(1).lower()
        j = next_code_index(lines, i + 1, min(len(lines), i + 10))
        if j is None or code(lines[j]) != "ld l, a":
            continue

        kind: str | None = None

        if "256-byte-aligned table: low byte is index" in lines[j]:
            k = next_code_index(lines, j + 1, min(len(lines), j + 5))
            if k is not None and code(lines[k]) == "ld a, [hl]":
                kind = "prg"

        if kind is None and any(
            "dead host-flag scaffold removed: zero-page $00 index" in lines[k]
            for k in range(i + 1, j)
        ):
            kind = "zp0"

        if kind is None:
            p = prev_code_index(lines, i - 1, max(0, i - 4))
            page_marker = any(
                "dead host-flag scaffold removed: page-aligned index" in lines[k]
                for k in range(j + 1, min(len(lines), j + 6))
            )
            if p is not None and PAGE_BASE_RE.fullmatch(code(lines[p])) and page_marker:
                kind = "page"

        if kind is None:
            continue

        ind = indent_of(lines[i])
        lines[i] = f"{ind}; DE cached index moved directly into L\n"
        if kind == "prg":
            lines[j] = f"{ind}ld l, {reg} ; DE-cached X/Y + aligned PRG table\n"
            prg += 1
        elif kind == "page":
            lines[j] = f"{ind}ld l, {reg} ; DE-cached X/Y + page-aligned RAM base\n"
            page += 1
        else:
            lines[j] = f"{ind}ld l, {reg} ; DE-cached X/Y + zero-page $00 base\n"
            zp0 += 1

    return prg, page, zp0


def optimize(lines: list[str]) -> tuple[int, int, int, int, int, int, int, int, int, int]:
    cached_blocks = cached_state = cached_zp = 0
    replaced_loads = mirrored_stores = load_seeds = store_seeds = 0

    for block in reversed(blocks(lines)):
        body = lines[block.start + 1 : block.end]
        if has_call(body):
            continue

        free_regs = [r for r in ("d", "e") if not uses_de_reg(body, r)]
        if not free_regs:
            continue

        candidates: list[tuple[int, str, str | int]] = []

        for state in ("nes_x", "nes_y", "nes_a"):
            loads, stores, first = state_stats(body, state)
            gain = hram_gain(loads, stores, first)
            if gain > 0:
                candidates.append((gain, "state", state))

        zp_allowed = not any(INDIRECT_MEM_RE.search(code(line)) for line in body)
        if zp_allowed:
            for addr, (loads, stores, bad, first) in zp_stats(body).items():
                if bad or loads == 0:
                    continue
                gain = zp_gain(loads, stores, first)
                if gain > 0:
                    candidates.append((gain, "zp", addr))

        if not candidates:
            continue

        candidates.sort(key=lambda item: item[0], reverse=True)
        chosen = candidates[: len(free_regs)]
        assignment: dict[tuple[str, str | int], str] = {
            (kind, key): reg for (_gain, kind, key), reg in zip(chosen, free_regs)
        }
        initialized: set[tuple[str, str | int]] = set()

        i = block.start + 1
        while i < block.end:
            c = code(lines[i])
            handled = False

            for state in ("nes_x", "nes_y", "nes_a"):
                key = ("state", state)
                reg = assignment.get(key)
                if reg is None:
                    continue
                kind = state_access(lines[i], state)
                if kind is None:
                    continue

                ind = indent_of(lines[i])
                if kind == "load":
                    if key in initialized:
                        lines[i] = f"{ind}ld a, {reg} ; DE cache {state}\n"
                        replaced_loads += 1
                    else:
                        lines[i] = lines[i] + f"{ind}ld {reg}, a ; seed DE cache {state}\n"
                        initialized.add(key)
                        load_seeds += 1
                else:
                    was_initialized = key in initialized
                    lines[i] = lines[i] + f"{ind}ld {reg}, a ; refresh DE cache {state}\n"
                    mirrored_stores += 1
                    initialized.add(key)
                    if not was_initialized:
                        store_seeds += 1
                handled = True
                break

            if handled:
                i += 1
                continue

            if m := ZP_LOAD_RE.fullmatch(c):
                addr = int(m.group(1), 16)
                key = ("zp", addr)
                reg = assignment.get(key)
                if reg is not None:
                    ind = indent_of(lines[i])
                    if key in initialized:
                        lines[i] = f"{ind}ld a, {reg} ; DE cache NES ZP ${addr:02X}\n"
                        replaced_loads += 1
                    else:
                        lines[i] = lines[i] + f"{ind}ld {reg}, a ; seed DE cache NES ZP ${addr:02X}\n"
                        initialized.add(key)
                        load_seeds += 1
                    i += 1
                    continue

            if m := ZP_STORE_RE.fullmatch(c):
                addr = int(m.group(1), 16)
                key = ("zp", addr)
                reg = assignment.get(key)
                if reg is not None and m.group(2).lower() == "a":
                    ind = indent_of(lines[i])
                    was_initialized = key in initialized
                    lines[i] = lines[i] + f"{ind}ld {reg}, a ; refresh DE cache NES ZP ${addr:02X}\n"
                    mirrored_stores += 1
                    initialized.add(key)
                    if not was_initialized:
                        store_seeds += 1

            i += 1

        cached_blocks += 1
        cached_state += sum(1 for kind, _key in assignment if kind == "state")
        cached_zp += sum(1 for kind, _key in assignment if kind == "zp")

    prg, page, zp0 = fold_direct_cached_indexes(lines)
    return (
        cached_blocks,
        cached_state,
        cached_zp,
        replaced_loads,
        mirrored_stores,
        load_seeds,
        store_seeds,
        prg,
        page,
        zp0,
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("asm", type=Path)
    args = p.parse_args()

    lines = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    stats = optimize(lines)
    args.asm.write_text("".join(lines), encoding="utf-8")
    (
        blocks_n,
        state_n,
        zp_n,
        loads_n,
        stores_n,
        load_seeds,
        store_seeds,
        prg,
        page,
        zp0,
    ) = stats
    print(
        f"de-cache: cached {state_n} CPU-state + {zp_n} ZP value(s) across {blocks_n} blocks, "
        f"replaced {loads_n} loads, mirrored {stores_n} stores; "
        f"seeded {load_seeds} on first load / {store_seeds} from prior store; "
        f"fed {prg}/{page}/{zp0} cached X/Y index(es) directly into PRG/page/ZP00 addresses"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
