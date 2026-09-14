#!/usr/bin/env python3
"""Fuse terminal CMP/CPX/CPY -> BCC/BCS through the host carry flag.

The compare-fold pass currently computes exact 6502 C from the GB borrow flag,
materializes it in nes_c_shadow, then terminal BCC/BCS reload that byte just to
branch.  For an immediately adjacent compare/carry-branch pair we can preserve
GB C while still publishing the canonical shadow, then let the branch consume
GB C directly.

A conservative inter-block carry liveness pass may also remove the canonical C
store when every successor overwrites C before any read.  NMI poll prologues are
carry users because a pending translated NMI materializes/pushes P before the
first source instruction in the successor.

No 6502 instruction, stack operation, branch destination, or NMI safe point is
moved. Unexpected/generated shapes are left untouched.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path

BLOCK_RE = re.compile(r"^nes_([0-9A-Fa-f]{4}):$")
INSN_RE = re.compile(
    r"; \$([0-9A-Fa-f]{4}): \$([0-9A-Fa-f]{2}) ([A-Za-z0-9_]+) ([A-Za-z0-9_]+)"
)
TARGET_RE = re.compile(r"\bnes_([0-9A-Fa-f]{4})\b")

BRANCHES = {"Bcc", "Bcs", "Beq", "Bmi", "Bne", "Bpl", "Bvc", "Bvs"}
COMPARES = {"Cmp", "Cpx", "Cpy"}
C_READERS = {"Bcc", "Bcs", "Adc", "Sbc", "Rol", "Ror", "Php", "Brk"}
C_WRITERS = {
    "Cmp", "Cpx", "Cpy", "Adc", "Sbc", "Asl", "Lsr", "Rol", "Ror",
    "Clc", "Sec", "Plp", "Rti",
}
DYNAMIC_EXITS = {"Jsr", "Rts", "Brk"}


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
    c_use: bool = False
    c_def: bool = False
    unknown_exit: bool = False
    nmi_poll: bool = False


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

    labels_set = set(blocks)
    for block in blocks.values():
        first_insn_i = block.insns[0].line if block.insns else block.end_i
        prologue = "".join(lines[block.label_i + 1:first_insn_i])
        block.nmi_poll = "nes_poll_nmi_hl" in prologue or "nes_host_vblank_pending" in prologue

        c_defined = False
        if block.nmi_poll:
            block.c_use = True

        for insn in block.insns:
            m = insn.mnemonic
            if m in C_READERS and not c_defined:
                block.c_use = True
            if m in C_WRITERS:
                c_defined = True
                block.c_def = True
            if m in DYNAMIC_EXITS or (m == "Jmp" and insn.mode == "Indirect"):
                block.unknown_exit = True

        if not block.insns:
            block.unknown_exit = True
            continue

        last = block.insns[-1]
        tail = "".join(lines[last.line + 1:block.end_i])
        refs = {
            int(m.group(1), 16)
            for m in TARGET_RE.finditer(tail)
            if int(m.group(1), 16) in labels_set
        }

        if last.mnemonic in BRANCHES:
            fallthrough = (last.pc + 2) & 0xFFFF
            if fallthrough in labels_set:
                block.succ.add(fallthrough)
            block.succ.update(refs)
        elif last.mnemonic == "Jmp" and last.mode == "Absolute":
            block.succ.update(refs)
            if not refs:
                block.unknown_exit = True
        elif last.mnemonic in {"Rts", "Rti", "Brk", "Jsr"} or (
            last.mnemonic == "Jmp" and last.mode == "Indirect"
        ):
            if last.mnemonic == "Rti":
                block.unknown_exit = True
        else:
            block.succ.update(refs)
            if not refs and block.next_addr is not None:
                block.succ.add(block.next_addr)
            elif not refs and block.next_addr is None:
                block.unknown_exit = True

    return blocks


def carry_liveness(blocks: dict[int, Block]) -> tuple[dict[int, bool], dict[int, bool]]:
    live_in = {a: False for a in blocks}
    live_out = {a: False for a in blocks}
    changed = True
    while changed:
        changed = False
        for addr, block in reversed(list(blocks.items())):
            out = block.unknown_exit or any(live_in.get(s, True) for s in block.succ)
            inn = block.c_use or (out and not block.c_def)
            if out != live_out[addr] or inn != live_in[addr]:
                live_out[addr] = out
                live_in[addr] = inn
                changed = True
    return live_in, live_out


def next_code(lines: list[str], i: int, end: int) -> int | None:
    j = i
    while j < end:
        if code(lines[j]):
            return j
        j += 1
    return None


def optimize(lines: list[str], blocks: dict[int, Block]) -> tuple[int, int]:
    _, c_out = carry_liveness(blocks)
    fused = 0
    stores_removed = 0

    for block in blocks.values():
        if len(block.insns) < 2:
            continue
        producer = block.insns[-2]
        branch = block.insns[-1]
        if producer.mnemonic not in COMPARES or branch.mnemonic not in {"Bcc", "Bcs"}:
            continue

        # Find the exact folded compare tail. The only semantic change is
        # replacing RL A (which destroys GB C) with JR/INC, both of which leave
        # C unchanged while still materializing 0/1 in A for nes_c_shadow.
        ccf_i = zero_i = rl_i = cstore_i = None
        for j in range(producer.line + 1, branch.line):
            c = code(lines[j])
            if c == "ccf":
                ccf_i = j
            elif c == "ld a, $00" and ccf_i is not None:
                zero_i = j
            elif c == "rl a" and zero_i is not None:
                rl_i = j
            elif c == "ldh [nes_c_shadow], a" and rl_i is not None:
                cstore_i = j
                break
        if None in {ccf_i, zero_i, rl_i, cstore_i}:
            continue

        # Confirm the terminal branch still has the canonical carry reload/test
        # emitted by recompile.rs. Do not guess around an unfamiliar shape.
        load_i = next_code(lines, branch.line + 1, block.end_i)
        if load_i is None or code(lines[load_i]) != "ldh a, [nes_c_shadow]":
            continue
        and_i = next_code(lines, load_i + 1, block.end_i)
        if and_i is None or code(lines[and_i]) != "and a":
            continue
        skip_i = next_code(lines, and_i + 1, block.end_i)
        expected = "jr nz, :+" if branch.mnemonic == "Bcc" else "jr z, :+"
        if skip_i is None or code(lines[skip_i]) != expected:
            continue

        ind = lines[rl_i][: len(lines[rl_i]) - len(lines[rl_i].lstrip())]
        label = f".cmpcarry_{producer.pc:04X}_zero"
        lines[rl_i] = (
            f"{ind}; preserve GB C while materializing exact 6502 carry shadow\n"
            f"{ind}jr nc, {label}\n"
            f"{ind}inc a\n"
            f"{label}:\n"
        )

        bind = lines[load_i][: len(lines[load_i]) - len(lines[load_i].lstrip())]
        lines[load_i] = f"{bind}; fused carry branch: GB C already is 6502 C\n"
        lines[and_i] = f"{bind}; canonical carry reload/test removed\n"
        direct_skip = "c" if branch.mnemonic == "Bcc" else "nc"
        lines[skip_i] = f"{bind}jr {direct_skip}, :+\n"
        fused += 1

        # The terminal branch consumes C from the host flag. Canonical C is only
        # needed after the branch. If all successors kill it before any use (and
        # none begin with an NMI poll), the shadow store itself is dead.
        if not c_out[block.addr]:
            cind = lines[cstore_i][: len(lines[cstore_i]) - len(lines[cstore_i].lstrip())]
            lines[cstore_i] = f"{cind}; dead terminal C shadow store removed\n"
            stores_removed += 1

    return fused, stores_removed


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("asm", type=Path)
    args = p.parse_args()

    lines = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    blocks = parse_blocks(lines)
    fused, removed = optimize(lines, blocks)
    args.asm.write_text("".join(lines), encoding="utf-8")
    print(
        f"carry-branch: fused {fused} terminal CMP/CPX/CPY -> BCC/BCS pairs, "
        f"removed {removed} dead C shadow stores"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
