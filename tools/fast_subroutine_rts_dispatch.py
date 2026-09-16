#!/usr/bin/env python3
"""Extend guarded RTS dispatch to multi-block 6502 subroutines.

`fast_leaf_rts_dispatch.py` only knows that a JSR target itself is a one-block
RTS leaf. Real NES subroutines often branch through several basic blocks before
reaching one or more RTS sites. This pass follows only the caller-visible
control flow of each statically known JSR target:

- branches/jumps/fallthrough stay inside the candidate subroutine graph;
- nested JSRs follow their normal continuation, not the nested callee;
- RTS blocks terminate the walk;
- indirect jumps/RTI/BRK terminate conservatively.

For every reached RTS block we collect exact continuation PCs from direct JSR
sites that can enter that subroutine. The *actual* return PC is still popped
from the emulated 6502 stack first. We only bypass `nes_dispatch_hl` when HL
matches one of those exact PCs; every mismatch falls back to the original
dispatcher. Thus stack tricks, dynamic entries and analysis misses preserve the
old behavior.

If an RTS can be reached from more static continuations than the guard-size
limit, rank continuations by the number of direct JSR sites producing each
return PC and specialize only the hottest subset. Unselected return PCs still
fall through to `nes_dispatch_hl`, so this broadens coverage without weakening
the exact-return guard or increasing its maximum size.

Within the selected exact-return set, emit the hottest high-byte group first
and then the hottest low-byte return inside that group. This changes only guard
ordering: coverage, code-size limits, exact comparisons, and dynamic fallback
remain unchanged.

This pass runs after the one-block leaf pass, so already-specialized RTS sites
are left alone. A per-bank expansion budget keeps the prototype from consuming
translated-code headroom too aggressively.
"""

from __future__ import annotations

import argparse
import collections
import re
from dataclasses import dataclass
from pathlib import Path


SECTION_BANK_RE = re.compile(r"^SECTION .*BANK\[(\d+)\]")
BLOCK_LABEL_RE = re.compile(r"^nes_([0-9A-Fa-f]{4})(?:_trace)?:$")
CANON_LABEL_RE = re.compile(r"^nes_([0-9A-Fa-f]{4}):$")
INSN_RE = re.compile(
    r"; \$([0-9A-Fa-f]{4}): \$([0-9A-Fa-f]{2}) ([A-Za-z0-9_]+) ([A-Za-z0-9_]+)"
)
TARGET_RE = re.compile(r"\bnes_([0-9A-Fa-f]{4})\b")

BRANCHES = {"Bcc", "Bcs", "Beq", "Bmi", "Bne", "Bpl", "Bvc", "Bvs"}
HARD_STOPS = {"Rti", "Brk"}


def code(line: str) -> str:
    return line.split(";", 1)[0].strip()


@dataclass
class Block:
    addr: int
    bank: int
    label_i: int
    end_i: int
    insns: list[tuple[int, int, str, str]]  # line, pc, mnemonic, mode


def parse_blocks(lines: list[str]) -> tuple[dict[int, Block], dict[int, int]]:
    # Physical translated bodies may begin at either nes_XXXX: or
    # nes_XXXX_trace:.  A later canonical nes_XXXX: adapter can share the same
    # NES address but contains no source instruction comments.  Segment on every
    # physical entry label, then retain the code-bearing segment for each address.
    labels: list[tuple[int, int, int]] = []
    label_bank: dict[int, int] = {}
    bank: int | None = None

    for i, line in enumerate(lines):
        sm = SECTION_BANK_RE.match(code(line))
        if sm:
            bank = int(sm.group(1))
        c = code(line)
        lm = BLOCK_LABEL_RE.fullmatch(c)
        if lm and bank is not None:
            addr = int(lm.group(1), 16)
            labels.append((i, addr, bank))
        cm = CANON_LABEL_RE.fullmatch(c)
        if cm and bank is not None:
            label_bank[int(cm.group(1), 16)] = bank

    blocks: dict[int, Block] = {}
    for n, (label_i, addr, block_bank) in enumerate(labels):
        end_i = labels[n + 1][0] if n + 1 < len(labels) else len(lines)
        for j in range(label_i + 1, end_i):
            if code(lines[j]).startswith("SECTION "):
                end_i = j
                break

        insns: list[tuple[int, int, str, str]] = []
        for j in range(label_i + 1, end_i):
            m = INSN_RE.search(lines[j])
            if m:
                insns.append((j, int(m.group(1), 16), m.group(3), m.group(4)))
        if insns:
            # Exactly one physical segment should carry translated source for a
            # given NES address.  Canonical adapters are intentionally ignored.
            prev = blocks.get(addr)
            assert prev is None, f"multiple code-bearing segments for NES ${addr:04X}"
            blocks[addr] = Block(addr, block_bank, label_i, end_i, insns)

    return blocks, label_bank


def tail_targets(lines: list[str], block: Block) -> set[int]:
    if not block.insns:
        return set()
    start = block.insns[-1][0] + 1
    found: set[int] = set()
    for j in range(start, block.end_i):
        for m in TARGET_RE.finditer(code(lines[j])):
            found.add(int(m.group(1), 16))
    return found


def direct_jsr_calls(
    lines: list[str], blocks: dict[int, Block], labels: set[int]
) -> dict[int, collections.Counter[int]]:
    """Map direct static JSR target -> weighted exact continuation PCs."""
    continuations: dict[int, collections.Counter[int]] = collections.defaultdict(collections.Counter)

    for block in blocks.values():
        for k, (comment_i, pc, mnemonic, _mode) in enumerate(block.insns):
            if mnemonic != "Jsr":
                continue
            end_i = block.insns[k + 1][0] if k + 1 < len(block.insns) else block.end_i
            targets: set[int] = set()
            for j in range(comment_i + 1, end_i):
                for m in TARGET_RE.finditer(code(lines[j])):
                    t = int(m.group(1), 16)
                    if t in blocks:
                        targets.add(t)
            if len(targets) != 1:
                continue
            target = next(iter(targets))
            ret_pc = (pc + 3) & 0xFFFF
            if ret_pc not in labels:
                # Non-returning inline dispatchers deliberately have no normal
                # continuation and cannot contribute an RTS return hint.
                continue
            continuations[target][ret_pc] += 1

    return continuations


def successors(lines: list[str], block: Block, labels: set[int]) -> set[int]:
    """Caller-visible successors while walking one subroutine body."""
    if not block.insns:
        return set()

    _line, pc, mnemonic, mode = block.insns[-1]
    if mnemonic == "Rts" or mnemonic in HARD_STOPS:
        return set()
    if mnemonic == "Jmp" and mode == "Indirect":
        return set()
    if mnemonic == "Jsr":
        ret_pc = (pc + 3) & 0xFFFF
        return {ret_pc} if ret_pc in labels else set()

    # For a branch this includes both the taken target emitted with the branch
    # and the block fallthrough emitted after it. For absolute JMP or a block
    # split at an already-known entry point it naturally yields the sole edge.
    return {t for t in tail_targets(lines, block) if t in labels}


def reachable_rts(
    entry: int, lines: list[str], blocks: dict[int, Block], labels: set[int]
) -> set[int]:
    seen: set[int] = set()
    todo = [entry]
    out: set[int] = set()

    while todo:
        addr = todo.pop()
        if addr in seen:
            continue
        seen.add(addr)
        block = blocks.get(addr)
        if block is None or not block.insns:
            continue
        if block.insns[-1][2] == "Rts":
            out.add(addr)
            continue
        for nxt in successors(lines, block, labels):
            if nxt not in seen:
                todo.append(nxt)
    return out


def find_generic_rts_dispatch(lines: list[str], block: Block) -> int | None:
    if not block.insns or block.insns[-1][2] != "Rts":
        return None
    rts_comment = block.insns[-1][0]
    saw_inc = False
    for j in range(rts_comment + 1, block.end_i):
        c = code(lines[j])
        if c == "inc hl":
            saw_inc = True
        elif c == "jp nes_dispatch_hl" and saw_inc:
            return j
    return None


def direct_jump(ret_pc: int, source_bank: int, label_bank: dict[int, int], ind: str) -> list[str]:
    target_bank = label_bank[ret_pc]
    if target_bank == source_bank:
        return [f"{ind}jp nes_{ret_pc:04X}\n"]
    return [
        f"{ind}ld a, ${target_bank:02X}\n",
        f"{ind}ld hl, nes_{ret_pc:04X}\n",
        f"{ind}jp nes_jump_known_hl_a\n",
    ]


def fast_dispatch(
    block: Block,
    returns: list[int],
    weights: collections.Counter[int],
    label_bank: dict[int, int],
) -> str:
    """Group by high byte, then order groups/returns by static JSR weight."""
    ind = "    "
    grouped: dict[int, list[int]] = collections.defaultdict(list)
    for ret in returns:
        grouped[(ret >> 8) & 0xFF].append(ret)

    ordered_groups = sorted(
        grouped.items(),
        key=lambda item: (-sum(weights[ret] for ret in item[1]), item[0]),
    )

    out: list[str] = [f"{ind}; guarded multi-block RTS return fast path\n"]
    for hi_index, (hi, vals) in enumerate(ordered_groups):
        next_hi = f"nes_rts_sub_{block.addr:04X}_hi_{hi_index}_next"
        out.extend([
            f"{ind}ld a, h\n",
            f"{ind}cp ${hi:02X}\n",
            f"{ind}jr nz, {next_hi}\n",
        ])
        ordered_vals = sorted(vals, key=lambda ret: (-weights[ret], ret))
        for lo_index, ret_pc in enumerate(ordered_vals):
            next_lo = f"nes_rts_sub_{block.addr:04X}_{hi_index}_{lo_index}_next"
            out.extend([
                f"{ind}ld a, l\n",
                f"{ind}cp ${ret_pc & 0xFF:02X}\n",
                f"{ind}jr nz, {next_lo}\n",
            ])
            out.extend(direct_jump(ret_pc, block.bank, label_bank, ind))
            out.append(f"{next_lo}:\n")
        out.append(f"{next_hi}:\n")

    out.append(f"{ind}jp nes_dispatch_hl\n")
    return "".join(out)


def pessimistic_bytes(text: str) -> int:
    # Labels/comments cost nothing; three bytes per real instruction safely
    # overestimates the LR35902 encodings used here.
    n = 0
    for line in text.splitlines():
        c = code(line)
        if c and not c.endswith(":"):
            n += 3
    return max(0, n - 3)  # replacing an existing three-byte JP


def rank_returns(weighted: collections.Counter[int], limit: int) -> list[int]:
    """Choose the hottest exact return PCs, deterministic on equal weights."""
    return [
        ret
        for ret, _weight in sorted(weighted.items(), key=lambda item: (-item[1], item[0]))[:limit]
    ]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("asm", type=Path)
    p.add_argument("--max-returns", type=int, default=4)
    p.add_argument("--bank-budget", type=int, default=640,
                   help="maximum pessimistic extra bytes added per ROMX bank")
    p.add_argument("--max-rts-per-bank", type=int, default=12)
    args = p.parse_args()

    lines = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    blocks, label_bank = parse_blocks(lines)
    labels = set(label_bank)
    call_returns = direct_jsr_calls(lines, blocks, labels)

    rts_return_weight: dict[int, collections.Counter[int]] = collections.defaultdict(collections.Counter)
    for entry, weighted_returns in call_returns.items():
        for rts in reachable_rts(entry, lines, blocks, labels):
            rts_return_weight[rts].update(weighted_returns)

    # -covered_weight, addr, extra, text, selected_count, was_wide
    candidates: list[tuple[int, int, int, str, int, bool]] = []
    for addr, weighted_returns in rts_return_weight.items():
        block = blocks.get(addr)
        if block is None or find_generic_rts_dispatch(lines, block) is None:
            # The leaf pass may already have consumed this exact dispatch.
            continue
        if not weighted_returns:
            continue

        ordered = rank_returns(weighted_returns, args.max_returns)
        if not ordered:
            continue
        text = fast_dispatch(block, ordered, weighted_returns, label_bank)
        covered_weight = sum(weighted_returns[ret] for ret in ordered)
        candidates.append((
            -covered_weight,
            addr,
            pessimistic_bytes(text),
            text,
            len(ordered),
            len(weighted_returns) > len(ordered),
        ))

    selected: dict[int, tuple[str, int, bool]] = {}
    used: dict[int, int] = collections.defaultdict(int)
    count_in_bank: dict[int, int] = collections.defaultdict(int)
    for _neg_weight, addr, extra, text, nret, was_wide in sorted(candidates):
        bank = blocks[addr].bank
        if count_in_bank[bank] >= args.max_rts_per_bank:
            continue
        if used[bank] + extra > args.bank_budget:
            continue
        selected[addr] = (text, nret, was_wide)
        used[bank] += extra
        count_in_bank[bank] += 1

    rewrites: list[tuple[int, str, int, bool]] = []
    for addr, (text, nret, was_wide) in selected.items():
        dispatch_i = find_generic_rts_dispatch(lines, blocks[addr])
        if dispatch_i is not None:
            rewrites.append((dispatch_i, text, nret, was_wide))

    total_targets = 0
    wide_sets = 0
    for dispatch_i, text, nret, was_wide in sorted(rewrites, reverse=True):
        lines[dispatch_i] = text
        total_targets += nret
        wide_sets += int(was_wide)

    args.asm.write_text("".join(lines), encoding="utf-8")
    print(
        f"rts-subroutine: specialized {len(rewrites)} additional RTS blocks / "
        f"{total_targets} exact return targets; "
        f"{wide_sets} wide return set(s) trimmed to top {args.max_returns}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
