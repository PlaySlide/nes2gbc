#!/usr/bin/env python3
"""Specialize very conservative one-block 6502 leaf subroutines with host CALL/RET.

The normal translated JSR/RTS path preserves the virtual 6502 stack and returns
through dynamic NES-PC dispatch.  For a leaf that cannot observe the temporary
JSR stack depth, we can execute a private clone with LR35902 CALL/RET instead.

Safety constraints are intentionally strict:
- the callee is one basic block ending in RTS;
- every statically visible incoming control edge is a JSR;
- caller, callee, and JSR continuation are in the same ROMX bank;
- the leaf has no NMI poll safe-point;
- the leaf does not touch SP/stack helpers, direct stack-page RAM, or generic
  dynamic CPU-bus helpers that could resolve to the stack page.

The original translated callee is left untouched for dynamic dispatch.  Native
callers target a private same-bank clone whose final translated RTS is replaced
by RET.  We also reproduce JSR's two stack-memory writes while leaving nes_sp
unchanged: because the clone cannot observe SP and no translated NMI can be
entered inside it, this matches the externally visible state after RTS while
avoiding all SP update/pop/return-dispatch traffic.
"""

from __future__ import annotations

import argparse
import collections
import re
from dataclasses import dataclass
from pathlib import Path


LABEL_RE = re.compile(r"^(nes_([0-9A-Fa-f]{4})):$")
SECTION_BANK_RE = re.compile(r"^SECTION .*BANK\[(\d+)\]")
INSN_RE = re.compile(
    r"; \$([0-9A-Fa-f]{4}): \$([0-9A-Fa-f]{2}) ([A-Za-z0-9_]+) ([A-Za-z0-9_]+)"
)
DIRECT_TARGET_RE = re.compile(r"(?:jp|jr) (nes_([0-9A-Fa-f]{4}))$")
ANY_TARGET_RE = re.compile(r"\bnes_([0-9A-Fa-f]{4})\b")

STACK_MNEMONICS = {"Pha", "Php", "Pla", "Plp", "Tsx", "Txs"}


def code(line: str) -> str:
    return line.split(";", 1)[0].strip()


@dataclass
class Block:
    addr: int
    label_i: int
    end_i: int
    bank: int
    rts_comment_i: int | None
    insns: list[tuple[int, str, str, int]]  # (line, mnemonic, mode, pc)


@dataclass
class CallSite:
    block_addr: int
    bank: int
    comment_i: int
    end_i: int
    target: int
    continuation: int
    pushed_return: int


def parse_blocks(lines: list[str]) -> dict[int, Block]:
    labels: list[tuple[int, int, int]] = []  # line, addr, bank
    bank: int | None = None
    for i, line in enumerate(lines):
        sm = SECTION_BANK_RE.match(code(line))
        if sm:
            bank = int(sm.group(1))
        lm = LABEL_RE.match(code(line))
        if lm and bank is not None:
            labels.append((i, int(lm.group(2), 16), bank))

    blocks: dict[int, Block] = {}
    for n, (label_i, addr, bank) in enumerate(labels):
        end_i = labels[n + 1][0] if n + 1 < len(labels) else len(lines)
        # Stop before a SECTION that opens a non-block region (dispatch tables,
        # hot mirrors, etc.) even when there is no following nes_XXXX label yet.
        for j in range(label_i + 1, end_i):
            if code(lines[j]).startswith("SECTION "):
                end_i = j
                break

        insns: list[tuple[int, str, str, int]] = []
        rts_comment_i: int | None = None
        for j in range(label_i + 1, end_i):
            m = INSN_RE.search(lines[j])
            if not m:
                continue
            pc = int(m.group(1), 16)
            mnemonic = m.group(3)
            mode = m.group(4)
            insns.append((j, mnemonic, mode, pc))
            if mnemonic == "Rts":
                rts_comment_i = j
        blocks[addr] = Block(addr, label_i, end_i, bank, rts_comment_i, insns)
    return blocks


def instruction_segment_end(block: Block, insn_index: int) -> int:
    line_i = block.insns[insn_index][0]
    if insn_index + 1 < len(block.insns):
        return block.insns[insn_index + 1][0]
    return block.end_i


def direct_jsr_target(lines: list[str], start: int, end: int) -> int | None:
    found: set[int] = set()
    for j in range(start, end):
        m = DIRECT_TARGET_RE.fullmatch(code(lines[j]))
        if m:
            found.add(int(m.group(2), 16))
    return next(iter(found)) if len(found) == 1 else None


def collect_calls(lines: list[str], blocks: dict[int, Block]) -> list[CallSite]:
    calls: list[CallSite] = []
    for block in blocks.values():
        for k, (comment_i, mnemonic, _mode, pc) in enumerate(block.insns):
            if mnemonic != "Jsr":
                continue
            end_i = instruction_segment_end(block, k)
            target = direct_jsr_target(lines, comment_i + 1, end_i)
            if target is None:
                continue
            continuation = (pc + 3) & 0xFFFF
            calls.append(
                CallSite(
                    block_addr=block.addr,
                    bank=block.bank,
                    comment_i=comment_i,
                    end_i=end_i,
                    target=target,
                    continuation=continuation,
                    pushed_return=(pc + 2) & 0xFFFF,
                )
            )
    return calls


def visible_incoming_kinds(lines: list[str], blocks: dict[int, Block]) -> dict[int, set[str]]:
    """Classify static generated references by the 6502 instruction owning them."""
    incoming: dict[int, set[str]] = collections.defaultdict(set)
    for block in blocks.values():
        for k, (comment_i, mnemonic, _mode, _pc) in enumerate(block.insns):
            end_i = instruction_segment_end(block, k)
            for j in range(comment_i + 1, end_i):
                for m in ANY_TARGET_RE.finditer(code(lines[j])):
                    target = int(m.group(1), 16)
                    if target in blocks:
                        incoming[target].add(mnemonic)
    return incoming


def leaf_is_stack_insensitive(lines: list[str], block: Block) -> bool:
    if block.rts_comment_i is None or not block.insns:
        return False
    if block.insns[-1][1] != "Rts":
        return False
    if any(mnemonic in STACK_MNEMONICS for _, mnemonic, _, _ in block.insns[:-1]):
        return False

    body = "".join(lines[block.label_i + 1 : block.rts_comment_i])
    forbidden = (
        "nes_sp",
        "nes_stack_",
        "nes_poll_nmi_hl",
        "call nes_cpu_read",
        "call nes_cpu_write",
        "nes_unimplemented_operand",
    )
    if any(token in body for token in forbidden):
        return False

    # Direct accesses to mirrored NES $0100-$01FF appear at host $C100-$C1FF.
    if re.search(r"\$C1[0-9A-Fa-f]{2}", body):
        return False
    if re.search(r"ld h, \$C1\b", body):
        return False
    return True


def estimate_clone_bytes(lines: list[str], block: Block) -> int:
    # Deliberately pessimistic: most LR35902 instructions are 1-3 bytes.
    n = 0
    for line in lines[block.label_i + 1 : block.rts_comment_i + 1]:
        c = code(line)
        if not c or c.endswith(":") or c.startswith("IF ") or c == "ENDC":
            continue
        n += 3
    return n + 1  # RET


def make_clone(lines: list[str], block: Block) -> list[str]:
    label = f"nes_native_leaf_{block.addr:04X}"
    clone = [
        "\n",
        f'SECTION "Native leaf {block.addr:04X}", ROMX, BANK[{block.bank}]\n',
        f"{label}:\n",
    ]
    # Copy the original block prologue and all instructions before RTS.  Keep
    # PROFILE_TRACE/debug breadcrumbs: they are semantically harmless and make
    # diagnostic builds describe the same NES PC as the canonical block.
    clone.extend(lines[block.label_i + 1 : block.rts_comment_i])
    clone.append(f"    ; native leaf replacement for 6502 RTS at ${block.insns[-1][3]:04X}\n")
    clone.append("    ret\n")
    return clone


def rewrite_call(lines: list[str], site: CallSite) -> list[str]:
    hi = (site.pushed_return >> 8) & 0xFF
    lo = site.pushed_return & 0xFF
    ind = "    "
    return [
        f"{ind}; native leaf JSR ${site.target:04X}; preserve 6502 stack-memory footprint\n",
        f"{ind}PROFILE_INC nes_profile_jsr_push\n",
        f"{ind}ldh a, [nes_sp]\n",
        f"{ind}ld l, a\n",
        f"{ind}ld h, $C1\n",
        f"{ind}ld a, ${hi:02X}\n",
        f"{ind}ld [hl], a\n",
        f"{ind}dec l\n",
        f"{ind}ld a, ${lo:02X}\n",
        f"{ind}ld [hl], a\n",
        f"{ind}call nes_native_leaf_{site.target:04X}\n",
        f"{ind}jp nes_{site.continuation:04X}\n",
        "\n",
    ]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("asm", type=Path)
    p.add_argument("--bank-budget", type=int, default=768,
                   help="maximum pessimistic clone bytes added per ROMX bank")
    p.add_argument("--max-leaves-per-bank", type=int, default=6)
    args = p.parse_args()

    lines = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    blocks = parse_blocks(lines)
    calls = collect_calls(lines, blocks)
    incoming = visible_incoming_kinds(lines, blocks)

    calls_by_target: dict[int, list[CallSite]] = collections.defaultdict(list)
    for c in calls:
        calls_by_target[c.target].append(c)

    candidates: list[tuple[int, int, int]] = []  # -calls, target, size
    for target, sites in calls_by_target.items():
        leaf = blocks.get(target)
        if leaf is None or not leaf_is_stack_insensitive(lines, leaf):
            continue
        # Any visible branch/jump/fallthrough-style reference keeps the canonical
        # block generic. Only explicit JSR ownership is eligible for native clone.
        kinds = incoming.get(target, set())
        if kinds and kinds != {"Jsr"}:
            continue
        if any(s.bank != leaf.bank for s in sites):
            continue
        if any((blocks.get(s.continuation) is None or blocks[s.continuation].bank != leaf.bank) for s in sites):
            continue
        size = estimate_clone_bytes(lines, leaf)
        candidates.append((-len(sites), target, size))

    selected: set[int] = set()
    used: dict[int, int] = collections.defaultdict(int)
    leaves_in_bank: dict[int, int] = collections.defaultdict(int)
    for _neg_calls, target, size in sorted(candidates):
        bank = blocks[target].bank
        if leaves_in_bank[bank] >= args.max_leaves_per_bank:
            continue
        if used[bank] + size > args.bank_budget:
            continue
        selected.add(target)
        used[bank] += size
        leaves_in_bank[bank] += 1

    selected_sites = [c for c in calls if c.target in selected]
    # Rewrite from the bottom upward so stored line indexes remain valid.
    for site in sorted(selected_sites, key=lambda s: s.comment_i, reverse=True):
        lines[site.comment_i + 1 : site.end_i] = rewrite_call(lines, site)

    # Append private clones after all ordinary generated regions.  Original leaf
    # blocks remain unchanged for dispatch-table/dynamic entry.
    for target in sorted(selected):
        lines.extend(make_clone(lines, blocks[target]))

    args.asm.write_text("".join(lines), encoding="utf-8")
    print(
        f"native-leaf: cloned {len(selected)} conservative leaves, "
        f"rewrote {len(selected_sites)} JSR sites"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
