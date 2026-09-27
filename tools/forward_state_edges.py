#!/usr/bin/env python3
"""Forward A/X/Y in host A across very strict same-bank static block edges.

Canonical `nes_XXXX` entries remain unchanged for dynamic dispatch, NMI paths,
and any other caller.  A proven predecessor may instead jump to an alternate
entry immediately after the successor's canonical HRAM reload.  The predecessor
spill is removed only when the successor republishes the same canonical state
before any helper call, host control-flow join, or other possible observer.

A successful edge therefore removes both:
  * predecessor `ldh [nes_a/x/y], a` (3 M-cycles), and
  * successor `ldh a, [nes_a/x/y]` (3 M-cycles on the fast edge).

This pass is intentionally release-only; Makefile skips it for TRACE/PROFILE/
PROFILE_TRACE builds so instrumentation entry semantics remain conventional.
"""

from __future__ import annotations

import argparse
import collections
import re
from dataclasses import dataclass
from pathlib import Path

SECTION_BANK_RE = re.compile(r'^SECTION .*BANK\[(\d+)\]')
BLOCK_RE = re.compile(r'^nes_([0-9A-Fa-f]{4}):$')
TAIL_RE = re.compile(r'^(?:jp|jr) nes_([0-9A-Fa-f]{4})$')
LOAD_RE = re.compile(r'^ldh a, \[(nes_[axy])\]$')
STORE_RE = re.compile(r'^ldh \[(nes_[axy])\], a$')


def code(line: str) -> str:
    return line.split(';', 1)[0].strip()


@dataclass
class Block:
    addr: int
    bank: int
    label_i: int
    end_i: int


def parse_blocks(lines: list[str]) -> dict[int, Block]:
    labels: list[tuple[int, int, int]] = []
    bank: int | None = None
    for i, line in enumerate(lines):
        c = code(line)
        sm = SECTION_BANK_RE.match(c)
        if sm:
            bank = int(sm.group(1))
        lm = BLOCK_RE.match(c)
        if lm and bank is not None:
            labels.append((i, int(lm.group(1), 16), bank))

    blocks: dict[int, Block] = {}
    for n, (label_i, addr, b) in enumerate(labels):
        end_i = labels[n + 1][0] if n + 1 < len(labels) else len(lines)
        for j in range(label_i + 1, end_i):
            if code(lines[j]).startswith('SECTION '):
                end_i = j
                break
        blocks[addr] = Block(addr, b, label_i, end_i)
    return blocks


def release_code_indices(lines: list[str], block: Block) -> list[int]:
    """Real release instructions, omitting the compile-time profile-trace body."""
    out: list[int] = []
    skip_profile = False
    for i in range(block.label_i + 1, block.end_i):
        c = code(lines[i])
        if c == 'IF DEF(NES2GBC_PROFILE_TRACE)':
            skip_profile = True
            continue
        if skip_profile:
            if c == 'ENDC':
                skip_profile = False
            continue
        if c:
            out.append(i)
    return out


def target_contract(lines: list[str], block: Block) -> tuple[str, int, int] | None:
    """Return (state, canonical-load line, mandatory republish line)."""
    real = release_code_indices(lines, block)
    if not real:
        return None

    # Fast entry may not skip an NMI poll/debug/other prefix.  The canonical
    # state reload must literally be the first release instruction in the block.
    first_i = real[0]
    m = LOAD_RE.fullmatch(code(lines[first_i]))
    if not m:
        return None
    state = m.group(1)

    # From the skipped load onward, canonical state may be stale only through a
    # single straight-line region.  It must be republished before any helper,
    # branch/jump/return, join label, explicit state reread, or section boundary.
    for i in real[1:]:
        c = code(lines[i])
        if c == f'ldh [{state}], a':
            return state, first_i, i
        if c.endswith(':') or c.startswith('SECTION '):
            return None
        low = c.lower()
        if low.startswith(('call ', 'jr ', 'jp ', 'ret', 'reti')):
            return None
        if f'[{state}]' in c:
            return None
        if c.startswith('PROFILE_INC '):
            return None
    return None


def writes_a(c: str) -> bool:
    low = c.lower()
    if low.startswith(('ld a,', 'ldh a,', 'pop af')):
        return True
    if low in {'inc a', 'dec a', 'cpl', 'daa', 'rlca', 'rrca', 'rla', 'rra'}:
        return True
    if re.match(r'^(?:add|adc|sub|sbc|and|or|xor)(?: a,)?\b', low):
        return True
    if re.match(r'^(?:rl|rr|sla|sra|srl|swap|res \d+,|set \d+,) a$', low):
        return True
    return False


def source_store(lines: list[str], block: Block, transfer_i: int, state: str) -> int | None:
    """Find a matching canonical store while host A remains unchanged to exit."""
    for i in range(transfer_i - 1, block.label_i, -1):
        c = code(lines[i])
        if not c:
            continue
        if c == f'ldh [{state}], a':
            return i
        if c.endswith(':') or c.startswith('SECTION '):
            return None
        low = c.lower()
        if low.startswith(('call ', 'jr ', 'jp ', 'ret', 'reti')):
            return None
        if writes_a(c):
            return None
    return None


def fast_label(addr: int, state: str) -> str:
    return f'nes_{addr:04X}_fast_{state.removeprefix("nes_")}'


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument('asm', type=Path)
    args = p.parse_args()

    lines = args.asm.read_text(encoding='utf-8').splitlines(keepends=True)
    blocks = parse_blocks(lines)

    contracts: dict[int, tuple[str, int, int]] = {}
    for addr, block in blocks.items():
        c = target_contract(lines, block)
        if c is not None:
            contracts[addr] = c

    edits: list[tuple[int, int, str, int, str]] = []
    # (store_i, transfer_i, replacement, target, state)
    for src_addr, block in blocks.items():
        real = release_code_indices(lines, block)
        if not real:
            continue
        transfer_i = real[-1]
        tm = TAIL_RE.fullmatch(code(lines[transfer_i]))
        if not tm:
            continue
        target = int(tm.group(1), 16)
        target_block = blocks.get(target)
        contract = contracts.get(target)
        if target_block is None or contract is None:
            continue
        if target_block.bank != block.bank:
            continue

        state, _load_i, _commit_i = contract
        store_i = source_store(lines, block, transfer_i, state)
        if store_i is None:
            continue

        label = fast_label(target, state)
        replacement = f'    jp {label} ; forwarded {state} in host A across static edge\n'
        edits.append((store_i, transfer_i, replacement, target, state))

    # Multiple source edges may share one target fast entry.
    needed: dict[tuple[int, str], int] = {}
    counts: collections.Counter[str] = collections.Counter()
    for store_i, transfer_i, replacement, target, state in edits:
        lines[store_i] = f'    ; forwarded edge: deferred canonical {state} spill to successor\n'
        lines[transfer_i] = replacement
        load_i = contracts[target][1]
        needed[(target, state)] = load_i
        counts[state] += 1

    # Insert from bottom upward so original line indices remain valid.
    for (target, state), load_i in sorted(needed.items(), key=lambda item: item[1], reverse=True):
        lines.insert(load_i + 1, f'{fast_label(target, state)}:\n')

    args.asm.write_text(''.join(lines), encoding='utf-8')
    print(
        'state-edge: forwarded '
        f'{len(edits)} static edge(s) / {len(needed)} fast target entrie(s); '
        f'A={counts["nes_a"]} X={counts["nes_x"]} Y={counts["nes_y"]}; '
        f'removed {len(edits)} HRAM spill(s) + bypassed {len(edits)} HRAM reload(s)'
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
