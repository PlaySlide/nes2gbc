#!/usr/bin/env python3
"""Mirror hot indexed NROM/CNROM PRG tables into translated code banks.

Absolute,X / Absolute,Y reads from fixed mapper-0/3 PRG currently switch the
MBC away from translated code, read one byte, then restore the code bank.  For
frequently referenced indexed tables, duplicate a bounded 256-byte window into
the same ROMX bank as the translated block and read it locally instead.

The pass is deliberately bounded to four unique tables (1 KiB) per translated
code bank. The bank allocator already keeps conservative headroom, and this cap
keeps the experiment easy to back out if a title has unusually dense output.
"""

from __future__ import annotations

import argparse
import collections
import re
from dataclasses import dataclass
from pathlib import Path


def code(line: str) -> str:
    return line.split(";", 1)[0].strip()


def load_rom(path: Path) -> tuple[int, bytes] | None:
    data = path.read_bytes()
    if len(data) < 16 or data[:4] != b"NES\x1a":
        return None
    h = data[:16]
    mapper = (h[6] >> 4) | (h[7] & 0xF0)
    if (h[7] & 0x0C) == 0x08:
        mapper |= (h[8] & 0x0F) << 8
    if mapper not in (0, 3):
        return None
    if (h[7] & 0x0C) == 0x08 and (h[9] & 0x0F) == 0x0F:
        return None
    prg_len = h[4] * 0x4000
    if prg_len not in (0x4000, 0x8000):
        return None
    start = 16 + (512 if (h[6] & 0x04) else 0)
    end = start + prg_len
    if end > len(data):
        return None
    return mapper, data[start:end]


def prg_byte(prg: bytes, addr: int) -> int:
    off = addr - 0x8000
    off &= 0x3FFF if len(prg) == 0x4000 else 0x7FFF
    return prg[off]


@dataclass(frozen=True)
class Site:
    start: int
    end: int
    bank: int
    base: int
    index: str


SECTION_RE = re.compile(r'SECTION .*BANK\[(\d+)\]')
BASE_RE = re.compile(r"ld hl, \$([0-9A-Fa-f]{4})")
INDEX_RE = re.compile(r"ldh a, \[(nes_[xy])\]")


def find_sites(lines: list[str]) -> list[Site]:
    sites: list[Site] = []
    bank: int | None = None
    i = 0
    while i < len(lines):
        sm = SECTION_RE.search(code(lines[i]))
        if sm:
            bank = int(sm.group(1))
        if bank is None or bank < 40 or i + 17 >= len(lines):
            i += 1
            continue

        bm = BASE_RE.fullmatch(code(lines[i]))
        im = INDEX_RE.fullmatch(code(lines[i + 1])) if bm else None
        if not bm or not im:
            i += 1
            continue
        base = int(bm.group(1), 16)
        # Keep the whole 8-bit indexed window in CPU PRG space and avoid
        # 16-bit address wrap, where the generic bus semantics are required.
        if base < 0x8000 or base > 0xFF00:
            i += 1
            continue

        expected = [
            "add l",
            "ld l, a",
            "jr nc, :+",
            "inc h",
            ":",
            "ld a, h",
            "cp $20",
            "jr nc, :+",
            "and $07",
            "or $C0",
            "ld h, a",
            "ld a, [hl]",
            "jr :++",
            ":",
            "call nes_cpu_read",
            ":",
        ]
        if [code(lines[i + 2 + n]) for n in range(len(expected))] != expected:
            i += 1
            continue

        sites.append(Site(i, i + 17, bank, base, im.group(1)))
        i += 18
    return sites


def label_for(bank: int, base: int) -> str:
    return f"nes_hot_prg_b{bank:03d}_{base:04X}"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("asm", type=Path)
    p.add_argument("rom", type=Path)
    p.add_argument("--tables-per-bank", type=int, default=4)
    args = p.parse_args()

    loaded = load_rom(args.rom)
    if loaded is None:
        print("indexed-prg: skipped (PRG is not supported fixed NROM/CNROM)")
        return 0
    mapper, prg = loaded

    lines = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    sites = find_sites(lines)
    if not sites:
        print("indexed-prg: no fixed indexed PRG sites found")
        return 0

    counts: dict[int, collections.Counter[int]] = collections.defaultdict(collections.Counter)
    for s in sites:
        counts[s.bank][s.base] += 1

    selected: set[tuple[int, int]] = set()
    for bank, c in counts.items():
        # Prefer bases referenced by several static sites; tie-break by address
        # for deterministic generated output.
        ranked = sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))
        for base, _ in ranked[: max(0, args.tables_per_bank)]:
            selected.add((bank, base))

    rewritten = 0
    mirrored: set[tuple[int, int]] = set()
    for s in sites:
        if (s.bank, s.base) not in selected:
            continue
        label = label_for(s.bank, s.base)
        indent = lines[s.start][: len(lines[s.start]) - len(lines[s.start].lstrip())]
        replacement = (
            f"{indent}PROFILE_INC nes_profile_cpu_read\n"
            f"{indent}PROFILE_INC nes_profile_read_prg\n"
            f"{indent}; local mapper {mapper} indexed PRG mirror ${s.base:04X},{s.index[-1].upper()}\n"
            f"{indent}ld hl, {label}\n"
            f"{indent}ldh a, [{s.index}]\n"
            f"{indent}add l\n"
            f"{indent}ld l, a\n"
            f"{indent}jr nc, :+\n"
            f"{indent}inc h\n"
            ":\n"
            f"{indent}ld a, [hl]\n"
        )
        lines[s.start] = replacement
        for j in range(s.start + 1, s.end + 1):
            lines[j] = f"{indent}; indexed PRG bus path removed\n"
        rewritten += 1
        mirrored.add((s.bank, s.base))

    if mirrored:
        lines.append("\n; Same-bank mirrors for hot fixed-PRG indexed reads.\n")
        for bank, base in sorted(mirrored):
            label = label_for(bank, base)
            lines.append(
                f'SECTION "Hot PRG mirror b{bank} ${base:04X}", ROMX, BANK[{bank}]\n'
            )
            lines.append(f"{label}:\n")
            data = [prg_byte(prg, base + i) for i in range(256)]
            for off in range(0, 256, 16):
                chunk = ", ".join(f"${b:02X}" for b in data[off:off + 16])
                lines.append(f"    db {chunk}\n")
            lines.append("\n")

    args.asm.write_text("".join(lines), encoding="utf-8")
    print(
        f"indexed-prg: mirrored {len(mirrored)} tables / 256B, "
        f"rewrote {rewritten} indexed PRG read sites"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
