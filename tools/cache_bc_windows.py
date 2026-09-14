#!/usr/bin/env python3
"""Use B/C as short-lived residual caches between native B/C uses.

The validated whole-block B/C caches run first and retain priority.  A register
already holding one of those long-lived caches is reserved for the entire block.
For the remaining register(s), this pass treats every native B/C use, helper
call, and local host control-flow edge as a hard barrier.  Inside each resulting
straight-line window the register is otherwise dead and may cache one profitable
remaining value from canonical 6502 X/Y/A HRAM state or direct NES zero-page
WRAM ($C000-$C0FF).

This is deliberately a second-tier allocator: it runs after the whole-block
B/C caches and the validated windowed D/E cache, so it only harvests residual
reloads they could not cover.  Canonical state remains authoritative; stores
remain in place and refresh any active short-lived cache.

Both enabled GBC interrupt handlers preserve BC.  Calls and local host control
flow terminate windows, so runtime helper conventions and branch joins cannot
observe or depend on a short-lived cache.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path

BLOCK_RE = re.compile(r"^nes_([0-9A-Fa-f]{4}):$")
ZP_LOAD_RE = re.compile(r"ld a, \[\$C0([0-9A-Fa-f]{2})\]$", re.IGNORECASE)
ZP_STORE_RE = re.compile(r"ld \[\$C0([0-9A-Fa-f]{2})\], ([a-z0-9$]+)$", re.IGNORECASE)
INDIRECT_MEM_RE = re.compile(r"\[(?:hl|de|bc|hli|hld)\]", re.IGNORECASE)
CACHED_INDEX_RE = re.compile(
    r"ld a, ([bc])\s*;\s*BC window cache nes_([xy])$", re.IGNORECASE
)
PAGE_BASE_RE = re.compile(r"ld hl, \$[0-9A-Fa-f]{2}00$", re.IGNORECASE)


def code(line: str) -> str:
    return line.split(";", 1)[0].strip()


def comment(line: str) -> str:
    parts = line.split(";", 1)
    return parts[1].strip().lower() if len(parts) == 2 else ""


def indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def normalize(lines: list[str]) -> None:
    lines[:] = "".join(lines).splitlines(keepends=True)


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


def line_uses_reg(c: str, reg: str) -> bool:
    if not c:
        return False
    if re.search(r"\bbc\b", c, re.IGNORECASE):
        return True
    return re.search(rf"\b{reg}\b", c, re.IGNORECASE) is not None


def is_control_barrier(c: str) -> bool:
    if not c:
        return False
    if c.endswith(":"):
        return True
    low = c.lower()
    return low.startswith(("call ", "jr ", "jp ", "ret", "reti"))


def is_barrier(line: str, reg: str) -> bool:
    c = code(line)
    return is_control_barrier(c) or line_uses_reg(c, reg)


def has_long_lived_cache(body: list[str], reg: str) -> bool:
    for line in body:
        c = code(line)
        if not line_uses_reg(c, reg):
            continue
        cm = comment(line)
        if (
            "cached nes_" in cm
            or "seed nes_" in cm
            or "refresh nes_" in cm
            or "cached nes zp" in cm
            or "seed nes zp" in cm
            or "refresh nes zp" in cm
            or "cached 6502 a" in cm
            or "seed 6502 a" in cm
            or "refresh 6502 a" in cm
            or "cached x/y" in cm
        ):
            return True
    return False


def state_access(line: str, state: str) -> str | None:
    c = code(line).lower()
    s = state.lower()
    if c in {f"ldh a, [{s}]", f"ld a, [{s}]"}:
        return "load"
    if c in {f"ldh [{s}], a", f"ld [{s}], a"}:
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


@dataclass
class Stats:
    windows: int = 0
    state_values: int = 0
    zp_values: int = 0
    replaced_loads: int = 0
    mirrored_stores: int = 0
    load_seeds: int = 0
    store_seeds: int = 0
    reserved_blocks: int = 0


def optimize_window(lines: list[str], start: int, end: int, reg: str, stats: Stats) -> None:
    if end <= start:
        return
    body = lines[start:end]
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
        return

    candidates.sort(key=lambda item: item[0], reverse=True)
    _gain, kind, key = candidates[0]
    initialized = False

    for i in range(start, end):
        ind = indent_of(lines[i])
        if kind == "state":
            assert isinstance(key, str)
            access = state_access(lines[i], key)
            if access == "load":
                if initialized:
                    lines[i] = f"{ind}ld a, {reg} ; BC window cache {key}\n"
                    stats.replaced_loads += 1
                else:
                    lines[i] = lines[i] + f"{ind}ld {reg}, a ; seed BC window cache {key}\n"
                    initialized = True
                    stats.load_seeds += 1
            elif access == "store":
                was_initialized = initialized
                lines[i] = lines[i] + f"{ind}ld {reg}, a ; refresh BC window cache {key}\n"
                stats.mirrored_stores += 1
                initialized = True
                if not was_initialized:
                    stats.store_seeds += 1
        else:
            assert isinstance(key, int)
            c = code(lines[i])
            lm = ZP_LOAD_RE.fullmatch(c)
            if lm and int(lm.group(1), 16) == key:
                if initialized:
                    lines[i] = f"{ind}ld a, {reg} ; BC window cache NES ZP ${key:02X}\n"
                    stats.replaced_loads += 1
                else:
                    lines[i] = lines[i] + f"{ind}ld {reg}, a ; seed BC window cache NES ZP ${key:02X}\n"
                    initialized = True
                    stats.load_seeds += 1
                continue
            sm = ZP_STORE_RE.fullmatch(c)
            if sm and int(sm.group(1), 16) == key and sm.group(2).lower() == "a":
                was_initialized = initialized
                lines[i] = lines[i] + f"{ind}ld {reg}, a ; refresh BC window cache NES ZP ${key:02X}\n"
                stats.mirrored_stores += 1
                initialized = True
                if not was_initialized:
                    stats.store_seeds += 1

    stats.windows += 1
    if kind == "state":
        stats.state_values += 1
    else:
        stats.zp_values += 1


def optimize_reg(lines: list[str], reg: str, stats: Stats) -> None:
    for block in reversed(blocks(lines)):
        body = lines[block.start + 1:block.end]
        if has_long_lived_cache(body, reg):
            stats.reserved_blocks += 1
            continue
        window_start = block.start + 1
        for i in range(block.start + 1, block.end):
            if is_barrier(lines[i], reg):
                optimize_window(lines, window_start, i, reg, stats)
                window_start = i + 1
        optimize_window(lines, window_start, block.end, reg, stats)


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
        lines[i] = f"{ind}; BC window cached index moved directly into L\n"
        if kind == "prg":
            lines[j] = f"{ind}ld l, {reg} ; BC-window X/Y + aligned PRG table\n"
            prg += 1
        elif kind == "page":
            lines[j] = f"{ind}ld l, {reg} ; BC-window X/Y + page-aligned RAM base\n"
            page += 1
        else:
            lines[j] = f"{ind}ld l, {reg} ; BC-window X/Y + zero-page $00 base\n"
            zp0 += 1
    return prg, page, zp0


def optimize(lines: list[str]) -> tuple[Stats, int, int, int]:
    stats = Stats()
    optimize_reg(lines, "b", stats)
    normalize(lines)
    optimize_reg(lines, "c", stats)
    normalize(lines)
    prg, page, zp0 = fold_direct_cached_indexes(lines)
    return stats, prg, page, zp0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("asm", type=Path)
    args = p.parse_args()

    lines = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    s, prg, page, zp0 = optimize(lines)
    args.asm.write_text("".join(lines), encoding="utf-8")
    print(
        f"bc-window: cached {s.state_values} CPU-state + {s.zp_values} ZP value(s) "
        f"across {s.windows} straight-line window(s), replaced {s.replaced_loads} loads, "
        f"mirrored {s.mirrored_stores} stores; seeded {s.load_seeds} on first load / "
        f"{s.store_seeds} from prior store; reserved {s.reserved_blocks} long-lived-cache "
        f"register/block pair(s); fed {prg}/{page}/{zp0} cached X/Y index(es) directly "
        f"into PRG/page/ZP00 addresses"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
