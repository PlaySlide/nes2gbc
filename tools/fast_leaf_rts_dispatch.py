#!/usr/bin/env python3
"""Fast-path exact RTS return targets without changing 6502 stack semantics.

A previous native CALL/RET experiment was fast but unsafe because it changed the
observable 6502 stack/control-flow behavior.  This pass leaves JSR, the virtual
stack, the callee, and RTS pops completely untouched.  It only replaces the
final dynamic dispatch of a very simple one-block RTS callee with guarded direct
jumps to statically known JSR continuations.

The guard compares the *actual* return PC popped from the emulated 6502 stack.
If it differs for any reason (stack tricks, dynamic entry, stale/static analysis,
etc.), execution falls back to nes_dispatch_hl exactly as before.
"""

from __future__ import annotations

import argparse
import collections
import re
from dataclasses import dataclass
from pathlib import Path


SECTION_BANK_RE = re.compile(r"^SECTION .*BANK\[(\d+)\]")
BLOCK_LABEL_RE = re.compile(r"^nes_([0-9A-Fa-f]{4}):$")
INSN_RE = re.compile(
    r"; \$([0-9A-Fa-f]{4}): \$([0-9A-Fa-f]{2}) ([A-Za-z0-9_]+) ([A-Za-z0-9_]+)"
)
TARGET_RE = re.compile(r"\bnes_([0-9A-Fa-f]{4})\b")


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
    labels: list[tuple[int, int, int]] = []
    label_bank: dict[int, int] = {}
    bank: int | None = None

    for i, line in enumerate(lines):
        sm = SECTION_BANK_RE.match(code(line))
        if sm:
            bank = int(sm.group(1))
        lm = BLOCK_LABEL_RE.match(code(line))
        if lm and bank is not None:
            addr = int(lm.group(1), 16)
            labels.append((i, addr, bank))
            label_bank[addr] = bank

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
        blocks[addr] = Block(addr, block_bank, label_i, end_i, insns)

    return blocks, label_bank


def insn_end(block: Block, index: int) -> int:
    if index + 1 < len(block.insns):
        return block.insns[index + 1][0]
    return block.end_i


def collect_jsr_returns(lines: list[str], blocks: dict[int, Block], labels: set[int]) -> dict[int, list[int]]:
    """Map direct static JSR target -> exact continuation PCs."""
    returns: dict[int, list[int]] = collections.defaultdict(list)

    for block in blocks.values():
        for k, (comment_i, pc, mnemonic, _mode) in enumerate(block.insns):
            if mnemonic != "Jsr":
                continue
            end_i = insn_end(block, k)
            targets: set[int] = set()
            for j in range(comment_i + 1, end_i):
                for m in TARGET_RE.finditer(code(lines[j])):
                    t = int(m.group(1), 16)
                    if t in blocks:
                        targets.add(t)
            if len(targets) != 1:
                continue
            target = next(iter(targets))
            continuation = (pc + 3) & 0xFFFF
            if continuation not in labels:
                # Non-returning JSR dispatchers intentionally have no normal
                # continuation block and must not participate in this pass.
                continue
            if continuation not in returns[target]:
                returns[target].append(continuation)

    return returns


def fast_dispatch(leaf: Block, continuations: list[int], label_bank: dict[int, int]) -> str:
    ind = "    "
    out: list[str] = [f"{ind}; guarded exact RTS return fast path\n"]

    for idx, ret_pc in enumerate(continuations):
        next_label = f"nes_rts_fast_{leaf.addr:04X}_{idx}_next"
        out.extend(
            [
                f"{ind}ld a, h\n",
                f"{ind}cp ${(ret_pc >> 8) & 0xFF:02X}\n",
                f"{ind}jr nz, {next_label}\n",
                f"{ind}ld a, l\n",
                f"{ind}cp ${ret_pc & 0xFF:02X}\n",
                f"{ind}jr nz, {next_label}\n",
            ]
        )

        target_bank = label_bank[ret_pc]
        if target_bank == leaf.bank:
            out.append(f"{ind}jp nes_{ret_pc:04X}\n")
        else:
            out.extend(
                [
                    f"{ind}ld a, ${target_bank:02X}\n",
                    f"{ind}ld hl, nes_{ret_pc:04X}\n",
                    f"{ind}jp nes_jump_known_hl_a\n",
                ]
            )
        out.append(f"{next_label}:\n")

    out.append(f"{ind}jp nes_dispatch_hl\n")
    return "".join(out)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("asm", type=Path)
    p.add_argument("--max-returns", type=int, default=4,
                   help="skip leaf RTS blocks with more static continuations than this")
    args = p.parse_args()

    lines = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    blocks, label_bank = parse_blocks(lines)
    labels = set(label_bank)
    returns = collect_jsr_returns(lines, blocks, labels)

    rewrites: list[tuple[int, str, int]] = []  # line, replacement, number of returns
    for target, continuations in returns.items():
        leaf = blocks.get(target)
        if leaf is None or not leaf.insns:
            continue
        # A CFG block cannot contain a branch/JSR before its terminator. Requiring
        # its final decoded instruction to be RTS therefore selects true straight-
        # line one-block leaf callees, but stack instructions inside remain legal.
        if leaf.insns[-1][2] != "Rts":
            continue
        if not (1 <= len(continuations) <= args.max_returns):
            continue

        rts_comment_i = leaf.insns[-1][0]
        dispatch_i: int | None = None
        saw_inc_hl = False
        for j in range(rts_comment_i + 1, leaf.end_i):
            c = code(lines[j])
            if c == "inc hl":
                saw_inc_hl = True
            elif c == "jp nes_dispatch_hl" and saw_inc_hl:
                dispatch_i = j
                break
        if dispatch_i is None:
            continue

        ordered = sorted(continuations)
        rewrites.append((dispatch_i, fast_dispatch(leaf, ordered, label_bank), len(ordered)))

    total_targets = 0
    for dispatch_i, replacement, nret in sorted(rewrites, reverse=True):
        lines[dispatch_i] = replacement
        total_targets += nret

    args.asm.write_text("".join(lines), encoding="utf-8")
    print(
        f"rts-fast: specialized {len(rewrites)} one-block RTS leaves / "
        f"{total_targets} exact return targets"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
