#!/usr/bin/env python3
"""Repack generated translated-code sections for better CFG bank locality.

The Rust emitter initially packs blocks by NES address. That is deterministic and
safe, but CFG neighbors can straddle GBC ROM banks and pay a translated-bank
switch on every static transfer.

This pass is deliberately conservative about semantics:

* Existing direct same-bank JP/JR edges are treated as "must stay together".
  The pass never turns one of those transfers into a cross-bank transfer.
* Existing cross-bank transfers are the only edges used as locality opportunities.
* If a formerly cross-bank edge becomes same-bank, keep the original helper's
  XOR A host-flag normalization before jumping directly to the target.
* Cross-bank bank immediates are rewritten to the target's new bank.
* Packing uses the emitter's exact conservative estimate:
      64 + 96 * NES instructions
  per translated basic block, and never exceeds the same $3000 budget.
* Optimization starts from the known-good original packing and accepts only
  positive-score single moves or pair swaps, so bank count never grows.

Run this before other generated-ASM peepholes.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path

BANK_BUDGET = 0x3000

SECTION_RE = re.compile(
    r'^(\s*)SECTION\s+"NES block ([0-9A-Fa-f]{4})",\s*ROMX,\s*BANK\[(\d+)\]\s*$'
)
ANY_SECTION_RE = re.compile(r"^\s*SECTION\b")
LABEL_RE = re.compile(r"^\s*nes_([0-9A-Fa-f]{4}):\s*$")
NES_INSN_RE = re.compile(r"^\s*;\s*\$[0-9A-Fa-f]{4}:")
DIRECT_RE = re.compile(r"^(?:jp|jr)(?:\s+[a-z]+,)?\s+nes_([0-9A-Fa-f]{4})$")
BANK_IMM_RE = re.compile(r"^ld a, \$([0-9A-Fa-f]{2})$")
TARGET_RE = re.compile(r"^ld hl, nes_([0-9A-Fa-f]{4})$")


def code(line: str) -> str:
    return line.split(";", 1)[0].strip()


def prev_code(lines: list[str], start: int) -> int | None:
    i = start
    while i >= 0:
        if code(lines[i]):
            return i
        i -= 1
    return None


@dataclass
class Section:
    sid: int
    start: int
    end: int
    header_addr: int
    original_bank: int
    labels: list[int]
    cost: int


class DSU:
    def __init__(self, n: int) -> None:
        self.p = list(range(n))

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def parse_sections(lines: list[str]) -> tuple[list[Section], dict[int, int], list[int | None]]:
    starts: list[tuple[int, int, int]] = []
    for i, line in enumerate(lines):
        m = SECTION_RE.match(code(line))
        if m:
            starts.append((i, int(m.group(2), 16), int(m.group(3))))

    sections: list[Section] = []
    label_to_section: dict[int, int] = {}
    line_section: list[int | None] = [None] * len(lines)

    for sid, (start, header_addr, bank) in enumerate(starts):
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if ANY_SECTION_RE.match(code(lines[j])):
                end = j
                break

        labels: list[int] = []
        insns = 0
        for j in range(start + 1, end):
            lm = LABEL_RE.match(code(lines[j]))
            if lm:
                labels.append(int(lm.group(1), 16))
            if NES_INSN_RE.match(lines[j]):
                insns += 1
            line_section[j] = sid
        line_section[start] = sid

        if not labels:
            raise ValueError(f"NES block section at line {start + 1} has no nes_XXXX label")

        cost = len(labels) * 64 + insns * 96
        sec = Section(sid, start, end, header_addr, bank, labels, cost)
        sections.append(sec)
        for label in labels:
            if label in label_to_section:
                raise ValueError(f"duplicate translated label nes_{label:04X}")
            label_to_section[label] = sid

    return sections, label_to_section, line_section


def helper_edges(
    lines: list[str],
    label_to_section: dict[int, int],
    line_section: list[int | None],
) -> list[tuple[int, int, int, int, int]]:
    """Return (bank_line, hl_line, jp_line, source_section, target_section)."""
    out = []
    for i, line in enumerate(lines):
        if code(line) != "jp nes_jump_known_hl_a":
            continue
        src = line_section[i]
        if src is None:
            continue
        hl_i = prev_code(lines, i - 1)
        if hl_i is None:
            continue
        tm = TARGET_RE.fullmatch(code(lines[hl_i]))
        if not tm:
            continue
        bank_i = prev_code(lines, hl_i - 1)
        if bank_i is None or not BANK_IMM_RE.fullmatch(code(lines[bank_i])):
            continue
        target = int(tm.group(1), 16)
        dst = label_to_section.get(target)
        if dst is None:
            continue
        out.append((bank_i, hl_i, i, src, dst))
    return out


def build_components(
    lines: list[str],
    sections: list[Section],
    label_to_section: dict[int, int],
    line_section: list[int | None],
) -> tuple[list[list[int]], dict[int, int]]:
    dsu = DSU(len(sections))

    # Preserve every direct same-bank relation emitted by the compiler.
    for i, line in enumerate(lines):
        src = line_section[i]
        if src is None:
            continue
        dm = DIRECT_RE.fullmatch(code(line))
        if not dm:
            continue
        dst = label_to_section.get(int(dm.group(1), 16))
        if dst is not None:
            dsu.union(src, dst)

    groups: dict[int, list[int]] = {}
    for sec in sections:
        groups.setdefault(dsu.find(sec.sid), []).append(sec.sid)

    components = list(groups.values())
    components.sort(key=lambda members: min(sections[s].header_addr for s in members))
    sec_to_comp: dict[int, int] = {}
    for cid, members in enumerate(components):
        for sid in members:
            sec_to_comp[sid] = cid

    # A must-link component should originate in one known-good bank.
    for members in components:
        banks = {sections[s].original_bank for s in members}
        if len(banks) != 1:
            raise ValueError(
                "direct same-bank component unexpectedly spans original banks: "
                + ", ".join(str(b) for b in sorted(banks))
            )

    return components, sec_to_comp


def optimize_layout(
    sections: list[Section],
    components: list[list[int]],
    sec_to_comp: dict[int, int],
    helper_refs: list[tuple[int, int, int, int, int]],
) -> tuple[list[int], int, int]:
    n = len(components)
    cost = [sum(sections[s].cost for s in members) for members in components]
    original = [sections[members[0]].original_bank for members in components]
    bank = original.copy()

    used: dict[int, int] = {}
    for cid, b in enumerate(bank):
        used[b] = used.get(b, 0) + cost[cid]
    for b, nbytes in used.items():
        if nbytes > BANK_BUDGET:
            raise ValueError(
                f"original bank {b} estimated at ${nbytes:04X}, exceeds ${BANK_BUDGET:04X}"
            )

    # Count static cross-bank opportunities. Multiple references increase weight.
    adj = [dict() for _ in range(n)]
    for _, _, _, src_sec, dst_sec in helper_refs:
        a, b = sec_to_comp[src_sec], sec_to_comp[dst_sec]
        if a == b:
            continue
        adj[a][b] = adj[a].get(b, 0) + 1
        adj[b][a] = adj[b].get(a, 0) + 1

    def conn(cid: int, b: int) -> int:
        return sum(w for other, w in adj[cid].items() if bank[other] == b)

    moves = 0
    swaps = 0
    all_banks = sorted(used)

    # Hill-climb from the known-good packing. Deterministic tie breaks.
    for _ in range(max(1, n * 8)):
        best = None

        # Positive single-component moves into existing headroom.
        for a in range(n):
            src = bank[a]
            before = conn(a, src)
            for dst in all_banks:
                if dst == src or used.get(dst, 0) + cost[a] > BANK_BUDGET:
                    continue
                delta = conn(a, dst) - before
                if delta <= 0:
                    continue
                key = (delta, 1, -cost[a], -a, -dst)
                if best is None or key > best[1]:
                    best = (0, key, a, dst)

        # Positive pair swaps can improve locality even when banks are full.
        for a in range(n):
            ba = bank[a]
            for b in range(a + 1, n):
                bb = bank[b]
                if ba == bb:
                    continue
                if used[ba] - cost[a] + cost[b] > BANK_BUDGET:
                    continue
                if used[bb] - cost[b] + cost[a] > BANK_BUDGET:
                    continue

                wab = adj[a].get(b, 0)
                before = conn(a, ba) + conn(b, bb)
                after = (conn(a, bb) - wab) + (conn(b, ba) - wab)
                delta = after - before
                if delta <= 0:
                    continue
                key = (delta, 0, -(cost[a] + cost[b]), -a, -b)
                if best is None or key > best[1]:
                    best = (1, key, a, b)

        if best is None:
            break

        kind = best[0]
        if kind == 0:
            _, _, a, dst = best
            src = bank[a]
            used[src] -= cost[a]
            used[dst] = used.get(dst, 0) + cost[a]
            bank[a] = dst
            moves += 1
        else:
            _, _, a, b = best
            ba, bb = bank[a], bank[b]
            used[ba] = used[ba] - cost[a] + cost[b]
            used[bb] = used[bb] - cost[b] + cost[a]
            bank[a], bank[b] = bb, ba
            swaps += 1

    return bank, moves, swaps


def rewrite(path: Path) -> tuple[int, int, int, int]:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    sections, label_to_section, line_section = parse_sections(lines)
    if not sections:
        print("bank-locality: no translated NES block sections found")
        return 0, 0, 0, 0

    refs = helper_edges(lines, label_to_section, line_section)
    components, sec_to_comp = build_components(
        lines, sections, label_to_section, line_section
    )
    comp_bank, moves, swaps = optimize_layout(
        sections, components, sec_to_comp, refs
    )

    changed_sections = 0
    new_sec_bank: dict[int, int] = {}
    for sec in sections:
        new_bank = comp_bank[sec_to_comp[sec.sid]]
        new_sec_bank[sec.sid] = new_bank
        if new_bank == sec.original_bank:
            continue
        changed_sections += 1
        old = lines[sec.start]
        indent = old[: len(old) - len(old.lstrip())]
        newline = "\n" if old.endswith("\n") else ""
        lines[sec.start] = (
            f'{indent}SECTION "NES block {sec.header_addr:04X}", ROMX, '
            f"BANK[{new_bank}]{newline}"
        )

    localized = 0
    updated_cross = 0
    for bank_i, hl_i, jp_i, src_sec, dst_sec in refs:
        src_bank = new_sec_bank[src_sec]
        dst_bank = new_sec_bank[dst_sec]
        tm = TARGET_RE.fullmatch(code(lines[hl_i]))
        if not tm:
            raise ValueError("known-transfer target changed unexpectedly during rewrite")
        target = int(tm.group(1), 16)

        indent = lines[bank_i][: len(lines[bank_i]) - len(lines[bank_i].lstrip())]
        newline = "\n" if lines[bank_i].endswith("\n") else ""

        if src_bank == dst_bank:
            # The old cross-bank helper ended in XOR A. Preserve that exact host
            # flag/A normalization even though the bank write is now unnecessary.
            lines[bank_i] = (
                f"{indent}xor a ; preserve prior cross-bank host-flag normalization{newline}"
            )
            lines[hl_i] = ""
            jp_indent = lines[jp_i][: len(lines[jp_i]) - len(lines[jp_i].lstrip())]
            jp_newline = "\n" if lines[jp_i].endswith("\n") else ""
            lines[jp_i] = (
                f"{jp_indent}jp nes_{target:04X} ; bank-local after CFG repack{jp_newline}"
            )
            localized += 1
        else:
            # Keep the helper path but retarget its bank immediate.
            lines[bank_i] = f"{indent}ld a, ${dst_bank:02X}{newline}"
            updated_cross += 1

    path.write_text("".join(lines), encoding="utf-8")
    print(
        "bank-locality: "
        f"{len(sections)} section(s), {len(components)} must-link component(s), "
        f"{moves} move(s), {swaps} swap(s), {changed_sections} section bank change(s), "
        f"{localized} cross-bank transfer(s) localized, "
        f"{updated_cross} cross-bank immediate(s) refreshed"
    )
    return changed_sections, localized, moves, swaps


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("asm", type=Path)
    args = p.parse_args()
    rewrite(args.asm)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
