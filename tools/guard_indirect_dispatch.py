#!/usr/bin/env python3
"""Guard statically-resolved 6502 indirect JMP target sets.

The CFG analyzer already recognizes several common NES jump-table idioms, but
translated `JMP (ptr)` still falls through the full dynamic PC dispatcher at
runtime. This post-pass recovers the same simple table shapes directly from the
ROM and guards a small set of likely targets.

Semantics remain guarded by the actual target produced by `nes_jmp_indirect_hl`:

* compare the computed 6502 PC in HL against exact generated target PCs;
* on an exact hit, jump directly (or use the existing known-bank helper);
* on every miss, fall back to `nes_dispatch_hl` unchanged.

Only target PCs that have an emitted `nes_XXXX` label are eligible. Wide target
sets are no longer discarded wholesale: choose up to `--max-targets` candidates
using duplicate table-entry frequency first, then static incoming generated-code
references as a tie-breaker. This changes only which exact targets get guards and
their order; every unselected real target still reaches the original dispatcher.
"""

from __future__ import annotations

import argparse
import collections
import re
from pathlib import Path

SECTION_BANK_RE = re.compile(r"^SECTION .*BANK\[(\d+)\]")
BLOCK_LABEL_RE = re.compile(r"^nes_([0-9A-Fa-f]{4}):$")
IMM_HL_RE = re.compile(r"^ld hl, \$([0-9A-Fa-f]{4})$")
INSN_RE = re.compile(
    r"; \$([0-9A-Fa-f]{4}): \$([0-9A-Fa-f]{2}) ([A-Za-z0-9_]+) ([A-Za-z0-9_]+)"
)
TARGET_RE = re.compile(r"\bnes_([0-9A-Fa-f]{4})\b")
MAX_WORD_TABLE_ENTRIES = 128


def code(line: str) -> str:
    return line.split(";", 1)[0].strip()


def prev_code(lines: list[str], start: int, floor: int = 0) -> int | None:
    for i in range(start, floor - 1, -1):
        if code(lines[i]):
            return i
    return None


def next_code(lines: list[str], start: int, ceiling: int | None = None) -> int | None:
    end = len(lines) if ceiling is None else min(ceiling, len(lines))
    for i in range(start, end):
        if code(lines[i]):
            return i
    return None


class NesRom:
    def __init__(self, path: Path):
        raw = path.read_bytes()
        if len(raw) < 16 or raw[:4] != b"NES\x1a":
            raise ValueError("expected iNES/NES 2.0 ROM")
        flags6 = raw[6]
        flags7 = raw[7]
        self.mapper = (flags6 >> 4) | (flags7 & 0xF0)
        if self.mapper not in (0, 3):
            raise ValueError(
                f"guarded indirect dispatch currently supports mapper 0/3, not {self.mapper}"
            )
        trainer = 512 if flags6 & 0x04 else 0
        prg_len = raw[4] * 0x4000
        start = 16 + trainer
        self.prg = raw[start : start + prg_len]
        if len(self.prg) not in (0x4000, 0x8000):
            raise ValueError(
                f"expected 16/32 KiB PRG, got {len(self.prg) // 1024} KiB"
            )

    def off(self, addr: int) -> int | None:
        if addr < 0x8000:
            return None
        x = addr - 0x8000
        if len(self.prg) == 0x4000:
            return x & 0x3FFF
        if x < len(self.prg):
            return x
        return None

    def bytes_at(self, addr: int, n: int) -> bytes | None:
        off = self.off(addr)
        if off is None or off + n > len(self.prg):
            return None
        return self.prg[off : off + n]

    def word(self, addr: int) -> int | None:
        b = self.bytes_at(addr, 2)
        if b is None:
            return None
        return b[0] | (b[1] << 8)


def index_labels(lines: list[str]) -> tuple[dict[int, int], list[int | None]]:
    label_bank: dict[int, int] = {}
    line_bank: list[int | None] = []
    bank: int | None = None
    for line in lines:
        c = code(line)
        if m := SECTION_BANK_RE.match(c):
            bank = int(m.group(1))
        line_bank.append(bank)
        if bank is not None and (m := BLOCK_LABEL_RE.fullmatch(c)):
            label_bank[int(m.group(1), 16)] = bank
    return label_bank, line_bank


def add_table_targets(
    rom: NesRom,
    base: int,
    labels: set[int],
    counts: collections.Counter[int],
    first_seen: dict[int, int],
) -> None:
    """Accumulate exact generated targets while retaining table frequency/order."""
    found_any = False
    for i in range(MAX_WORD_TABLE_ENTRIES):
        target = rom.word((base + i * 2) & 0xFFFF)
        if target is None:
            break
        if target not in labels:
            if found_any:
                break
            continue
        found_any = True
        counts[target] += 1
        if target not in first_seen:
            first_seen[target] = len(first_seen)


def resolve_indirect_targets(
    rom: NesRom,
    jmp_pc: int,
    pointer: int,
    labels: set[int],
) -> tuple[collections.Counter[int], dict[int, int]]:
    if pointer > 0x00FE:
        return collections.Counter(), {}

    tables: list[int] = []

    # Form 1:
    #   LDA table,Y / STA ptr / INY / LDA table,Y / STA ptr+1 / JMP (ptr)
    start = max(0x8000, jmp_pc - 64)
    for pc in range(start, max(start, jmp_pc - 10)):
        b = rom.bytes_at(pc, 11)
        if b is None:
            continue
        if (
            b[0] == 0xB9
            and b[3] == 0x85
            and b[4] == (pointer & 0xFF)
            and b[5] == 0xC8
            and b[6] == 0xB9
            and b[9] == 0x85
            and b[10] == ((pointer + 1) & 0xFF)
            and b[1] == b[7]
            and b[2] == b[8]
        ):
            base = b[1] | (b[2] << 8)
            if base not in tables:
                tables.append(base)

    # Form 2: caller builds ptr from table/table+1 then JSRs a tiny JMP(ptr)
    # trampoline. Scan physical PRG bytes so mirrored 16 KiB NROM works too.
    jsr_lo = jmp_pc & 0xFF
    jsr_hi = (jmp_pc >> 8) & 0xFF
    prg = rom.prg
    for o in range(0, max(0, len(prg) - 12)):
        b = prg[o : o + 13]
        if len(b) < 13:
            break
        if (
            b[0] == 0xB9
            and b[3] == 0x85
            and b[4] == (pointer & 0xFF)
            and b[5] == 0xB9
            and b[8] == 0x85
            and b[9] == ((pointer + 1) & 0xFF)
            and b[10] == 0x20
            and b[11] == jsr_lo
            and b[12] == jsr_hi
        ):
            base = b[1] | (b[2] << 8)
            high_base = b[6] | (b[7] << 8)
            if high_base == ((base + 1) & 0xFFFF) and base not in tables:
                tables.append(base)

    counts: collections.Counter[int] = collections.Counter()
    first_seen: dict[int, int] = {}
    for base in tables:
        add_table_targets(rom, base, labels, counts, first_seen)
    return counts, first_seen


def static_incoming_scores(lines: list[str], labels: set[int]) -> collections.Counter[int]:
    """Count generated-code static references as a secondary hotness signal."""
    scores: collections.Counter[int] = collections.Counter()
    for line in lines:
        c = code(line)
        if not (
            c.startswith("jr ")
            or c.startswith("jp ")
            or c.startswith("ld hl, nes_")
        ):
            continue
        for m in TARGET_RE.finditer(c):
            target = int(m.group(1), 16)
            if target in labels:
                scores[target] += 1
    return scores


def rank_targets(
    counts: collections.Counter[int],
    first_seen: dict[int, int],
    incoming: collections.Counter[int],
    limit: int,
) -> list[int]:
    """Prefer repeated table entries, then statically popular generated targets."""
    ranked = sorted(
        counts,
        key=lambda target: (
            -counts[target],
            -incoming[target],
            first_seen.get(target, 1 << 30),
            target,
        ),
    )
    return ranked[:limit]


def find_sites(
    lines: list[str], line_bank: list[int | None]
) -> list[tuple[int, int, int, int]]:
    """Return (dispatch line, JMP PC, pointer, source bank)."""
    sites: list[tuple[int, int, int, int]] = []
    for i, line in enumerate(lines):
        if code(line) != "call nes_jmp_indirect_hl":
            continue
        prev_i = prev_code(lines, i - 1, max(0, i - 6))
        next_i = next_code(lines, i + 1, min(len(lines), i + 6))
        if (
            prev_i is None
            or next_i is None
            or code(lines[next_i]) != "jp nes_dispatch_hl"
        ):
            continue
        m = IMM_HL_RE.fullmatch(code(lines[prev_i]))
        if not m:
            continue
        pointer = int(m.group(1), 16)

        jmp_pc: int | None = None
        for j in range(max(0, prev_i - 5), i + 1):
            im = INSN_RE.search(lines[j])
            if im and im.group(3) == "Jmp" and im.group(4) == "Indirect":
                jmp_pc = int(im.group(1), 16)
        bank = line_bank[i]
        if jmp_pc is not None and bank is not None:
            sites.append((next_i, jmp_pc, pointer, bank))
    return sites


def fast_dispatch(
    site_id: int,
    jmp_pc: int,
    source_bank: int,
    targets: list[int],
    target_weights: dict[int, int],
    label_bank: dict[int, int],
    indent: str,
) -> str:
    grouped: dict[int, list[int]] = collections.defaultdict(list)
    for target in targets:
        grouped[(target >> 8) & 0xFF].append(target)

    rank_index = {target: n for n, target in enumerate(targets)}
    groups = sorted(
        grouped.items(),
        key=lambda item: (
            -sum(target_weights[t] for t in item[1]),
            min(rank_index[t] for t in item[1]),
            item[0],
        ),
    )

    out = [
        f"{indent}; guarded resolved JMP(ind) fast path: {len(targets)} selected target(s)\n"
    ]
    for hi_n, (hi, vals) in enumerate(groups):
        next_hi = f"nes_indirect_fast_{jmp_pc:04X}_{site_id}_hi_{hi_n}_next"
        out.extend(
            [
                f"{indent}ld a, h\n",
                f"{indent}cp ${hi:02X}\n",
                f"{indent}jr nz, {next_hi}\n",
            ]
        )
        vals.sort(key=lambda t: rank_index[t])
        for lo_n, target in enumerate(vals):
            next_lo = (
                f"nes_indirect_fast_{jmp_pc:04X}_{site_id}_{hi_n}_{lo_n}_next"
            )
            out.extend(
                [
                    f"{indent}ld a, l\n",
                    f"{indent}cp ${target & 0xFF:02X}\n",
                    f"{indent}jr nz, {next_lo}\n",
                ]
            )
            target_bank = label_bank[target]
            if target_bank == source_bank:
                out.append(f"{indent}jp nes_{target:04X}\n")
            else:
                out.extend(
                    [
                        f"{indent}ld a, ${target_bank:02X}\n",
                        f"{indent}ld hl, nes_{target:04X}\n",
                        f"{indent}jp nes_jump_known_hl_a\n",
                    ]
                )
            out.append(f"{next_lo}:\n")
        out.append(f"{next_hi}:\n")
    out.append(f"{indent}jp nes_dispatch_hl\n")
    return "".join(out)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("asm", type=Path)
    p.add_argument("rom", type=Path)
    p.add_argument("--max-targets", type=int, default=4)
    args = p.parse_args()

    lines = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    label_bank, line_bank = index_labels(lines)
    labels = set(label_bank)
    rom = NesRom(args.rom)
    incoming = static_incoming_scores(lines, labels)

    sites = find_sites(lines, line_bank)
    rewrites: list[tuple[int, str]] = []
    resolved_sites = 0
    wide_sites = 0
    total_selected_targets = 0
    total_resolved_targets = 0

    for site_id, (dispatch_i, jmp_pc, pointer, source_bank) in enumerate(sites):
        counts, first_seen = resolve_indirect_targets(rom, jmp_pc, pointer, labels)
        if not counts:
            continue
        resolved_sites += 1
        total_resolved_targets += len(counts)
        if len(counts) > args.max_targets:
            wide_sites += 1

        targets = rank_targets(counts, first_seen, incoming, args.max_targets)
        if not targets:
            continue
        # Duplicate table-entry count is the primary signal. Static incoming refs
        # break ties while still keeping all behavior behind exact PC guards.
        target_weights = {
            target: counts[target] * 1024 + incoming[target] for target in targets
        }
        indent = lines[dispatch_i][
            : len(lines[dispatch_i]) - len(lines[dispatch_i].lstrip())
        ]
        rewrites.append(
            (
                dispatch_i,
                fast_dispatch(
                    site_id,
                    jmp_pc,
                    source_bank,
                    targets,
                    target_weights,
                    label_bank,
                    indent,
                ),
            )
        )
        total_selected_targets += len(targets)

    for dispatch_i, replacement in sorted(rewrites, reverse=True):
        lines[dispatch_i] = replacement

    args.asm.write_text("".join(lines), encoding="utf-8")
    print(
        f"indirect-fast: specialized {len(rewrites)}/{resolved_sites} resolved JMP(ind) site(s); "
        f"guarding {total_selected_targets}/{total_resolved_targets} exact target(s), "
        f"trimmed {wide_sites} wide set(s) to top {args.max_targets}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
