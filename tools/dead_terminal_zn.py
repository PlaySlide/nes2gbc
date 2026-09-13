#!/usr/bin/env python3
"""Remove provably dead terminal flag-shadow stores.

This pass is intentionally narrow. It only touches a basic block whose final
6502 instruction is BEQ/BNE and whose generated branch has already been fused
to test the producer result still held in host A. The immediately preceding
6502 instruction may therefore publish flag shadows solely for code *after* the
branch.

We run a conservative inter-block liveness analysis over the decoded-instruction
comments in generated.asm. NMI poll prologues count as flag users because a
pending NMI materializes/pushes P before the first 6502 instruction in that
block. Dynamic exits, JSR, RTS, BRK, PHP and unknown control flow keep flags
live. A store is removed only when every successor kills that flag before any
possible use.

For carry, the first optimization is even narrower: only terminal CMP/CPX/CPY
feeding BEQ/BNE may drop a dead nes_c_shadow store. The compare's host
instruction sequence is otherwise left byte-for-byte intact, so host flags and
branch behavior are unchanged; we merely avoid publishing an emulated flag that
provably cannot be observed.

No branch condition, 6502 instruction, stack state, or NMI safe point is moved.
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

# 6502 instructions that replace both Z and N with values independent of their
# incoming state. TXS deliberately absent; TSX deliberately present.
ZN_WRITERS = {
    "Lda", "Ldx", "Ldy", "Tax", "Tay", "Txa", "Tya", "Tsx", "Pla",
    "And", "Ora", "Eor", "Adc", "Sbc", "Cmp", "Cpx", "Cpy", "Bit",
    "Inc", "Dec", "Inx", "Iny", "Dex", "Dey", "Asl", "Lsr", "Rol", "Ror",
    "Plp", "Rti",
}
Z_READERS = {"Beq", "Bne", "Php", "Brk"}
N_READERS = {"Bmi", "Bpl", "Php", "Brk"}

# Carry readers are checked before writers while scanning a block because
# ADC/SBC/ROL/ROR consume incoming C and then replace it. CMP/CPX/CPY and shifts
# replace C without consuming its previous value; CLC/SEC are pure definitions.
C_READERS = {"Bcc", "Bcs", "Adc", "Sbc", "Rol", "Ror", "Php", "Brk"}
C_WRITERS = {
    "Clc", "Sec", "Adc", "Sbc", "Cmp", "Cpx", "Cpy",
    "Asl", "Lsr", "Rol", "Ror", "Plp", "Rti",
}

BRANCHES = {"Bcc", "Bcs", "Beq", "Bmi", "Bne", "Bpl", "Bvc", "Bvs"}
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
    z_use: bool = False
    n_use: bool = False
    z_def: bool = False
    n_def: bool = False
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

        z_defined = False
        n_defined = False
        if block.nmi_poll:
            # A pending translated NMI materializes P before the first source op.
            block.z_use = True
            block.n_use = True

        for insn in block.insns:
            m = insn.mnemonic
            if m in Z_READERS and not z_defined:
                block.z_use = True
            if m in N_READERS and not n_defined:
                block.n_use = True
            if m in ZN_WRITERS:
                z_defined = True
                n_defined = True
                block.z_def = True
                block.n_def = True
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
            # Dynamic/control-stack exits are conservatively handled by
            # unknown_exit above. RTI overwrites flags but its target is dynamic;
            # keeping unknown_exit true is harmlessly conservative.
            if last.mnemonic == "Rti":
                block.unknown_exit = True
        else:
            block.succ.update(refs)
            if not refs and block.next_addr is not None:
                block.succ.add(block.next_addr)
            elif not refs and block.next_addr is None:
                block.unknown_exit = True

    return blocks


def liveness(blocks: dict[int, Block], which: str) -> tuple[dict[int, bool], dict[int, bool]]:
    use = {a: (b.z_use if which == "z" else b.n_use) for a, b in blocks.items()}
    defs = {a: (b.z_def if which == "z" else b.n_def) for a, b in blocks.items()}
    live_in = {a: False for a in blocks}
    live_out = {a: False for a in blocks}

    changed = True
    while changed:
        changed = False
        for addr, block in reversed(list(blocks.items())):
            out = block.unknown_exit or any(live_in.get(s, True) for s in block.succ)
            inn = use[addr] or (out and not defs[addr])
            if out != live_out[addr] or inn != live_in[addr]:
                live_out[addr] = out
                live_in[addr] = inn
                changed = True
    return live_in, live_out


def carry_liveness(blocks: dict[int, Block]) -> tuple[dict[int, bool], dict[int, bool]]:
    """Conservative 6502 C liveness using source instructions and CFG edges."""
    use: dict[int, bool] = {}
    defs: dict[int, bool] = {}

    for addr, block in blocks.items():
        c_defined = False
        c_use = block.nmi_poll
        c_def = False
        for insn in block.insns:
            m = insn.mnemonic
            if m in C_READERS and not c_defined:
                c_use = True
            if m in C_WRITERS:
                c_defined = True
                c_def = True
        use[addr] = c_use
        defs[addr] = c_def

    live_in = {a: False for a in blocks}
    live_out = {a: False for a in blocks}
    changed = True
    while changed:
        changed = False
        for addr, block in reversed(list(blocks.items())):
            out = block.unknown_exit or any(live_in.get(s, True) for s in block.succ)
            inn = use[addr] or (out and not defs[addr])
            if out != live_out[addr] or inn != live_in[addr]:
                live_out[addr] = out
                live_in[addr] = inn
                changed = True
    return live_in, live_out


def optimize(lines: list[str], blocks: dict[int, Block]) -> tuple[int, int, int, int]:
    _, z_out = liveness(blocks, "z")
    _, n_out = liveness(blocks, "n")
    _, c_out = carry_liveness(blocks)
    z_removed = 0
    n_removed = 0
    c_removed = 0
    eligible = 0

    for block in blocks.values():
        if len(block.insns) < 2:
            continue
        branch = block.insns[-1]
        if branch.mnemonic not in {"Beq", "Bne"}:
            continue

        branch_segment = "".join(lines[branch.line + 1:block.end_i])
        if "fused Z branch: A already holds flag result" not in branch_segment:
            continue

        producer = block.insns[-2]
        if producer.mnemonic not in ZN_WRITERS:
            continue
        segment_start = producer.line + 1
        segment_end = branch.line
        eligible += 1

        z_idx = None
        n_idx = None
        c_idx = None
        for j in range(segment_start, segment_end):
            c = code(lines[j])
            if c == "ldh [nes_z_shadow], a":
                z_idx = j
            elif c == "ldh [nes_n_shadow], a":
                n_idx = j
            elif c == "ldh [nes_c_shadow], a":
                c_idx = j

        # The fused branch consumes Z from host A/GB flags, so the canonical Z
        # byte is needed only after the branch. N is not consumed by BEQ/BNE at
        # all. Remove either store only if inter-block dataflow proves it dead.
        if z_idx is not None and not z_out[block.addr]:
            ind = lines[z_idx][: len(lines[z_idx]) - len(lines[z_idx].lstrip())]
            lines[z_idx] = f"{ind}; dead terminal Z shadow store removed\n"
            z_removed += 1
        if n_idx is not None and not n_out[block.addr]:
            ind = lines[n_idx][: len(lines[n_idx]) - len(lines[n_idx].lstrip())]
            lines[n_idx] = f"{ind}; dead terminal N shadow store removed\n"
            n_removed += 1

        # CMP/CPX/CPY do not consume incoming C; they only publish a new C.
        # When neither branch successor can observe that value before another C
        # definition, skip only the canonical HRAM store. The surrounding GB
        # compare/carry materialization remains unchanged, preserving host flags.
        if (
            producer.mnemonic in {"Cmp", "Cpx", "Cpy"}
            and c_idx is not None
            and not c_out[block.addr]
        ):
            ind = lines[c_idx][: len(lines[c_idx]) - len(lines[c_idx].lstrip())]
            lines[c_idx] = f"{ind}; dead terminal compare C shadow store removed\n"
            c_removed += 1

    return eligible, z_removed, n_removed, c_removed


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("asm", type=Path)
    args = p.parse_args()

    lines = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    blocks = parse_blocks(lines)
    eligible, zr, nr, cr = optimize(lines, blocks)
    args.asm.write_text("".join(lines), encoding="utf-8")
    print(
        f"dead-zn: analyzed {eligible} fused terminal BEQ/BNE producers, "
        f"removed {zr} Z + {nr} N shadow stores, "
        f"{cr} dead terminal compare C shadow store(s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
