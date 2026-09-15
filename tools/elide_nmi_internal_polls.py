#!/usr/bin/env python3
"""Elide translated-NMI poll safe-points that are provably NMI-exclusive.

Generated code polls ``nes_host_vblank_pending`` at entry points and backward
loop targets so long-running translated mainline code can accept a host VBlank
as a NES NMI. The same poll sequence is dead weight inside blocks that can only
be reached from the translated NES NMI itself: nested NES NMIs are forbidden,
and the runtime does not publish another translated-NMI event while
``nes_nmi_active`` is set.

This pass proves exclusivity from the generated CFG:

* roots are read from ``nes_reset``, ``nes_nmi_entry``, and ``nes_irq_entry``;
* direct branches/jumps, JSR callees, and JSR continuations are followed;
* resolved indirect-jump targets emitted in a block tail are followed;
* if reset/IRQ reach an unresolved indirect JMP, the pass disables itself
  rather than assuming that unknown mainline control flow cannot enter an
  apparently NMI-only block.

Only the exact compiler-emitted poll prologue is removed. Blocks reachable
from reset or IRQ retain their original poll points, even when they are also
called from NMI code.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path

BLOCK_RE = re.compile(r"^nes_([0-9A-Fa-f]{4}):$")
INSN_RE = re.compile(
    r"; \\$([0-9A-Fa-f]{4}): \\$([0-9A-Fa-f]{2}) ([A-Za-z0-9_]+) ([A-Za-z0-9_]+)"
)
TARGET_RE = re.compile(r"\\bnes_([0-9A-Fa-f]{4})\\b")
LD_HL_IMM_RE = re.compile(r"ld hl, \\$([0-9A-Fa-f]{4})$", re.IGNORECASE)
BRANCHES = {"Bcc", "Bcs", "Beq", "Bmi", "Bne", "Bpl", "Bvc", "Bvs"}


def code(line: str) -> str:
    return line.split(";", 1)[0].strip()


@dataclass
class Insn:
    line: int
    pc: int
    mnemonic: str
    mode: str


@dataclass
class Block:
    addr: int
    label_i: int
    end_i: int
    next_addr: int | None
    insns: list[Insn]
    succ: set[int]
    unresolved_indirect: bool = False


def parse_blocks(lines: list[str]) -> dict[int, Block]:
    labels: list[tuple[int, int]] = []
    for i, line in enumerate(lines):
        m = BLOCK_RE.fullmatch(code(line))
        if m:
            labels.append((i, int(m.group(1), 16)))

    blocks: dict[int, Block] = {}
    for n, (label_i, addr) in enumerate(labels):
        raw_end = labels[n + 1][0] if n + 1 < len(labels) else len(lines)
        end_i = raw_end
        for j in range(label_i + 1, raw_end):
            if code(lines[j]).startswith("SECTION "):
                end_i = j
                break
        next_addr = labels[n + 1][1] if n + 1 < len(labels) and end_i == raw_end else None
        insns: list[Insn] = []
        for j in range(label_i + 1, end_i):
            m = INSN_RE.search(lines[j])
            if m:
                insns.append(Insn(j, int(m.group(1), 16), m.group(3), m.group(4)))
        blocks[addr] = Block(addr, label_i, end_i, next_addr, insns, set())

    label_set = set(blocks)
    for block in blocks.values():
        if not block.insns:
            continue
        last = block.insns[-1]
        tail = "".join(lines[last.line + 1 : block.end_i])
        refs = {
            int(m.group(1), 16)
            for m in TARGET_RE.finditer(tail)
            if int(m.group(1), 16) in label_set
        }

        if last.mnemonic in BRANCHES:
            fallthrough = (last.pc + 2) & 0xFFFF
            if fallthrough in label_set:
                block.succ.add(fallthrough)
            block.succ.update(refs)
        elif last.mnemonic == "Jsr":
            # Static JSR has two whole-program CFG paths: the callee itself and
            # the continuation after its eventual RTS.
            block.succ.update(refs)
            ret_pc = (last.pc + 3) & 0xFFFF
            if ret_pc in label_set:
                block.succ.add(ret_pc)
        elif last.mnemonic == "Jmp" and last.mode == "Absolute":
            block.succ.update(refs)
        elif last.mnemonic == "Jmp" and last.mode == "Indirect":
            block.succ.update(refs)
            block.unresolved_indirect = not bool(refs)
        elif last.mnemonic in {"Rts", "Rti", "Brk"}:
            pass
        else:
            block.succ.update(refs)
            if not refs and block.next_addr is not None:
                block.succ.add(block.next_addr)

    return blocks


def parse_root(lines: list[str], label: str) -> int | None:
    for i, line in enumerate(lines):
        if code(line) != label:
            continue
        for j in range(i + 1, min(len(lines), i + 12)):
            c = code(lines[j])
            m = LD_HL_IMM_RE.fullmatch(c)
            if m:
                return int(m.group(1), 16)
            if c.endswith(":") and j != i + 1:
                break
    return None


def reachable(blocks: dict[int, Block], roots: list[int]) -> set[int]:
    seen: set[int] = set()
    todo = [root for root in roots if root in blocks]
    while todo:
        addr = todo.pop()
        if addr in seen:
            continue
        seen.add(addr)
        block = blocks.get(addr)
        if block is None:
            continue
        for target in block.succ:
            if target in blocks and target not in seen:
                todo.append(target)
    return seen


def find_poll_span(lines: list[str], block: Block) -> tuple[int, int] | None:
    """Return [start,end) of the exact compiler-emitted poll prologue."""
    first_insn_i = block.insns[0].line if block.insns else block.end_i
    lo = block.label_i + 1
    hi = first_insn_i

    call_i = next(
        (i for i in range(lo, hi) if code(lines[i]) == "call nes_poll_nmi_hl"),
        None,
    )
    if call_i is None:
        return None

    start = None
    for i in range(call_i - 1, max(lo - 1, call_i - 8), -1):
        if code(lines[i]) == "ldh a, [nes_host_vblank_pending]":
            start = i
            break
    if start is None:
        return None

    if [code(lines[start + k]) for k in range(3)] != [
        "ldh a, [nes_host_vblank_pending]",
        "and a",
        "jr z, :+",
    ]:
        return None

    if call_i != start + 4 or not re.fullmatch(
        r"ld hl, \\$[0-9A-Fa-f]{4}", code(lines[start + 3])
    ):
        return None

    if call_i + 3 >= hi:
        return None
    if code(lines[call_i + 1]) != "and a":
        return None
    if code(lines[call_i + 2]) != "jp nz, nes_nmi_entry":
        return None
    if code(lines[call_i + 3]) != ":":
        return None

    return start, call_i + 4


def optimize(lines: list[str]) -> tuple[int, int, int, bool]:
    blocks = parse_blocks(lines)
    reset = parse_root(lines, "nes_reset:")
    nmi = parse_root(lines, "nes_nmi_entry:")
    irq = parse_root(lines, "nes_irq_entry:")
    if nmi is None:
        return 0, 0, 0, False

    nmi_reach = reachable(blocks, [nmi])
    other_roots = [root for root in (reset, irq) if root is not None and root != nmi]
    other_reach = reachable(blocks, other_roots)

    # Unknown mainline computed control could enter any translated block. In
    # that case we cannot prove NMI exclusivity from this generated CFG.
    unsafe_dynamic = any(
        blocks[addr].unresolved_indirect for addr in other_reach if addr in blocks
    )
    if unsafe_dynamic:
        return len(nmi_reach), 0, 0, True

    exclusive = nmi_reach - other_reach
    removed = 0
    for addr in sorted(exclusive):
        block = blocks.get(addr)
        span = find_poll_span(lines, block) if block is not None else None
        if span is None:
            continue
        start, end = span
        indent = lines[start][: len(lines[start]) - len(lines[start].lstrip())]
        lines[start] = (
            f"{indent}; NMI-exclusive safe-point poll elided: nested NES NMI impossible\n"
        )
        for i in range(start + 1, end):
            lines[i] = ""
        removed += 1

    return len(nmi_reach), len(exclusive), removed, False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("asm", type=Path)
    args = parser.parse_args()

    lines = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    nmi_blocks, exclusive_blocks, removed, unsafe = optimize(lines)
    args.asm.write_text("".join(lines), encoding="utf-8")

    if unsafe:
        print(
            "nmi-exclusive-polls: kept all polls; non-NMI control flow reaches "
            "an unresolved indirect JMP"
        )
    else:
        print(
            f"nmi-exclusive-polls: NMI reaches {nmi_blocks} block(s), "
            f"{exclusive_blocks} proven NMI-only; elided {removed} poll site(s)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
