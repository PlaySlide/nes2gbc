#!/usr/bin/env python3
"""Mirror hot indexed NROM/CNROM PRG tables into translated code banks.

Absolute,X / Absolute,Y reads from fixed mapper-0/3 PRG currently switch the
MBC away from translated code, read one byte, then restore the code bank. For
frequently referenced indexed tables, duplicate a bounded 256-byte window into
the same ROMX bank as the translated block and read it locally instead.

The pass is deliberately bounded to four unique tables (1 KiB) per translated
code bank. Mirrored tables for each bank share one 256-byte-aligned section.
Because every table is exactly 256 bytes, every table label then has low byte
$00. Indexed reads can therefore overwrite L directly with X/Y instead of
paying ADD/carry/high-byte-fixup address arithmetic at every access.
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
# Superblock/DE register caches of X/Y: the index is already in B/C/D/E.
REG_INDEX_RE = re.compile(r"ld a, ([bcde])")


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
        if bm and not im:
            im = REG_INDEX_RE.fullmatch(code(lines[i + 1]))
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


def index_name(index: str) -> str:
    return index[-1].upper() if index.startswith("nes_") else f"<{index}>"


MIRROR_LABEL_RE = re.compile(r"nes_hot_prg_b(\d{3})_([0-9A-F]{4}):")


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

    # A later invocation (after the X/Y/DE register-cache passes) reuses
    # tables mirrored by an earlier one and appends new tables to the bank's
    # existing mirror section.
    existing: set[tuple[int, int]] = set()
    for line in lines:
        m = MIRROR_LABEL_RE.fullmatch(code(line))
        if m:
            existing.add((int(m.group(1)), int(m.group(2), 16)))

    selected: set[tuple[int, int]] = set(existing)
    for bank, c in counts.items():
        # Prefer bases referenced by several static sites; tie-break by address
        # for deterministic generated output.
        ranked = sorted(
            ((b, n) for b, n in c.items() if (bank, b) not in existing),
            key=lambda kv: (-kv[1], kv[0]),
        )
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
            f"{indent}; aligned local mapper {mapper} indexed PRG mirror ${s.base:04X},{index_name(s.index)}\n"
            f"{indent}ld hl, {label}\n"
            + (
                f"{indent}ldh a, [{s.index}]\n"
                f"{indent}ld l, a ; 256-byte-aligned table: low byte is index\n"
                if s.index.startswith("nes_")
                else f"{indent}ld l, {s.index} ; cached index register; aligned table\n"
            )
            + f"{indent}ld a, [hl]\n"
        )
        lines[s.start] = replacement
        for j in range(s.start + 1, s.end + 1):
            lines[j] = f"{indent}; aligned indexed PRG bus/address path removed\n"
        rewritten += 1
        mirrored.add((s.bank, s.base))

    new_tables = mirrored - existing
    if new_tables:
        by_bank: dict[int, list[int]] = collections.defaultdict(list)
        for bank, base in sorted(new_tables):
            by_bank[bank].append(base)

        def table_lines(bank: int, base: int) -> list[str]:
            out = [f"{label_for(bank, base)}:\n"]
            data = [prg_byte(prg, base + i) for i in range(256)]
            for off in range(0, 256, 16):
                chunk = ", ".join(f"${b:02X}" for b in data[off:off + 16])
                out.append(f"    db {chunk}\n")
            out.append("\n")
            return out

        # Existing per-bank mirror sections: insert before the next SECTION.
        sec_hdr = re.compile(r'SECTION "Hot PRG mirrors b(\d+)", ROMX')
        inserts: dict[int, list[str]] = {}
        for idx, line in enumerate(lines):
            m = sec_hdr.match(code(line))
            if m and int(m.group(1)) in by_bank:
                bank = int(m.group(1))
                end = idx + 1
                while end < len(lines) and not code(lines[end]).startswith("SECTION"):
                    end += 1
                inserts[end] = [l for b in by_bank.pop(bank) for l in table_lines(bank, b)]
        for end in sorted(inserts, reverse=True):
            lines[end:end] = inserts[end]

        if by_bank:
            lines.append("\n; Same-bank 256-byte-aligned mirrors for hot fixed-PRG indexed reads.\n")
        for bank in sorted(by_bank):
            # One aligned section per bank bounds linker padding to at most 255
            # bytes for the whole group. Every 256-byte table after the first
            # remains naturally aligned.
            lines.append(
                f'SECTION "Hot PRG mirrors b{bank}", ROMX, BANK[{bank}], ALIGN[8]\n'
            )
            for base in by_bank[bank]:
                lines.extend(table_lines(bank, base))

    args.asm.write_text("".join(lines), encoding="utf-8")
    print(
        f"indexed-prg: mirrored {len(new_tables)} new aligned tables / 256B "
        f"({len(mirrored)} used), "
        f"rewrote {rewritten} indexed PRG read sites with direct low-byte indexing"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
