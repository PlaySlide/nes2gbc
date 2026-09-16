#!/usr/bin/env python3
"""Fast-path exact RTS return targets without changing 6502 stack semantics.

A previous native CALL/RET experiment was fast but unsafe because it changed the
observable 6502 stack/control-flow behavior. This pass leaves JSR, the virtual
stack, the callee, and RTS pops completely untouched. It only replaces the
final dynamic dispatch of a very simple one-block RTS callee with guarded direct
jumps to statically known JSR continuations.

The emulated 6502 stack contains PC-1 when RTS pops it. For specialized returns,
compare that raw stacked address against continuation-1 and jump directly. Only
an unmatched fallback performs the architectural RTS increment before entering
nes_dispatch_hl. Thus matched fast paths avoid one unconditional INC HL while
stack state and dynamic fallback semantics remain unchanged.

When a leaf has multiple exact static continuations, emit the continuation with
the most direct JSR sites first. This changes only guard ordering: the eligible
return set, guard count, exact comparisons, stack behavior and dynamic fallback
remain unchanged.
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


def insn_end(block: Block, index: int) -> int:
    if index + 1 < len(block.insns):
        return block.insns[index + 1][0]
    return block.end_i


def collect_jsr_returns(
    lines: list[str], blocks: dict[int, Block], labels: set[int]
) -> dict[int, collections.Counter[int]]:
    """Map direct static JSR target -> weighted exact continuation PCs."""
    returns: dict[int, collections.Counter[int]] = collections.defaultdict(collections.Counter)

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
            returns[target][continuation] += 1

    return returns


def fast_dispatch(leaf: Block, continuations: list[int], label_bank: dict[int, int]) -> str:
    ind = "    "
    out: list[str] = [f"{ind}; guarded exact RTS raw-stack return fast path\n"]

    for idx, ret_pc in enumerate(continuations):
        stacked_pc = (ret_pc - 1) & 0xFFFF
        next_label = f"nes_rts_fast_{leaf.addr:04X}_{idx}_next"
        out.extend(
            [
                f"{ind}ld a, h\n",
                f"{ind}cp ${(stacked_pc >> 8) & 0xFF:02X}\n",
                f"{ind}jr nz, {next_label}\n",
                f"{ind}ld a, l\n",
                f"{ind}cp ${stacked_pc & 0xFF:02X}\n",
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

    out.extend(
        [
            f"{ind}; unmatched RTS: apply the architectural PC+1 before dispatch\n",
            f"{ind}inc hl\n",
            f"{ind}jp nes_dispatch_hl\n",
        ]
    )
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

    rewrites: list[tuple[int, int, str, int]] = []  # inc line, dispatch line, replacement, returns
    for target, weighted_returns in returns.items():
        leaf = blocks.get(target)
        if leaf is None or not leaf.insns:
            continue
        # A CFG block cannot contain a branch/JSR before its terminator. Requiring
        # its final decoded instruction to be RTS therefore selects true straight-
        # line one-block leaf callees, but stack instructions inside remain legal.
        if leaf.insns[-1][2] != "Rts":
            continue
        if not (1 <= len(weighted_returns) <= args.max_returns):
            continue

        rts_comment_i = leaf.insns[-1][0]
        inc_i: int | None = None
        dispatch_i: int | None = None
        for j in range(rts_comment_i + 1, leaf.end_i):
            c = code(lines[j])
            if c == "inc hl" and inc_i is None:
                inc_i = j
            elif c == "jp nes_dispatch_hl" and inc_i is not None:
                dispatch_i = j
                break
        if inc_i is None or dispatch_i is None:
            continue

        ordered = [
            ret
            for ret, _weight in sorted(
                weighted_returns.items(), key=lambda item: (-item[1], item[0])
            )
        ]
        rewrites.append(
            (inc_i, dispatch_i, fast_dispatch(leaf, ordered, label_bank), len(ordered))
        )

    total_targets = 0
    for inc_i, dispatch_i, replacement, nret in sorted(rewrites, reverse=True):
        lines[inc_i] = replacement
        lines[dispatch_i] = "    ; generic RTS dispatch moved into guarded fallback above\n"
        total_targets += nret

    args.asm.write_text("".join(lines), encoding="utf-8")
    print(
        f"rts-fast: specialized {len(rewrites)} one-block RTS leaves / "
        f"{total_targets} exact return targets; deferred {len(rewrites)} RTS PC increment(s) to fallback"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
