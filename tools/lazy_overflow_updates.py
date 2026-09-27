#!/usr/bin/env python3
"""Skip dead 6502 overflow-state publication while preserving host flags.

ADC/SBC/BIT in the generated LR35902 currently update the canonical 6502 V bit
in `nes_p` on every execution.  Most NES code rarely reads V.  This pass runs an
instruction-level, inter-block liveness analysis over decoded source markers and
removes only V *state publication* when every path overwrites V before BVC/BVS,
PHP/BRK, a translated-NMI status materialization, or an unknown control escape
can observe it.

For ADC/SBC we deliberately keep the overflow-condition calculation even when V
is dead.  The old inline helper leaves LR35902 host flags dependent on that
condition; retaining the condition and only deleting the `nes_p` load/update
keeps those host flags identical.  Carry, Z and N publication is untouched.
BIT similarly keeps BIT/JR/OR so its host-flag exit shape is unchanged; only the
canonical V load/mask/store is removed.

This is intentionally conservative around JSR/RTS/BRK, indirect jumps, NMI poll
prologues and unrecognized exits.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path

BLOCK_RE = re.compile(r"^nes_([0-9A-Fa-f]{4}):$")
TRACE_LABEL_RE = re.compile(r"^nes_[0-9A-Fa-f]{4}_trace:$")
INSN_RE = re.compile(
    r"; \$([0-9A-Fa-f]{4}): \$([0-9A-Fa-f]{2}) ([A-Za-z0-9_]+) ([A-Za-z0-9_]+)"
)
TARGET_RE = re.compile(r"\bnes_([0-9A-Fa-f]{4})\b")

V_WRITERS = {"Adc", "Sbc", "Bit", "Clv", "Plp", "Rti"}
V_READERS = {"Bvc", "Bvs", "Php", "Brk", "Jsr", "Rts"}
BRANCHES = {"Bcc", "Bcs", "Beq", "Bmi", "Bne", "Bpl", "Bvc", "Bvs"}
DYNAMIC_EXITS = {"Jsr", "Rts", "Brk"}
OPTIMIZABLE = {"Adc", "Sbc", "Bit"}


def code(line: str) -> str:
    return line.split(";", 1)[0].strip()


def indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


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
    v_use: bool = False
    v_def: bool = False
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
            c = code(lines[j])
            if c.startswith("SECTION ") or TRACE_LABEL_RE.fullmatch(c):
                # Private trace labels are alternate CFG entries.  Do not let
                # inter-block liveness reason through them as if execution
                # could only arrive from the preceding canonical label.
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

        v_defined = False
        if block.nmi_poll:
            block.v_use = True

        for insn in block.insns:
            m = insn.mnemonic
            if m in V_READERS and not v_defined:
                block.v_use = True
            if m in V_WRITERS:
                v_defined = True
                block.v_def = True
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


def liveness(blocks: dict[int, Block]) -> tuple[dict[int, bool], dict[int, bool]]:
    live_in = {a: False for a in blocks}
    live_out = {a: False for a in blocks}

    changed = True
    while changed:
        changed = False
        for addr, block in reversed(list(blocks.items())):
            out = block.unknown_exit or any(live_in.get(s, True) for s in block.succ)
            inn = block.v_use or (out and not block.v_def)
            if out != live_out[addr] or inn != live_in[addr]:
                live_out[addr] = out
                live_in[addr] = inn
                changed = True
    return live_in, live_out


def dead_v_writers(blocks: dict[int, Block], live_out: dict[int, bool]) -> set[int]:
    dead_lines: set[int] = set()
    for block in blocks.values():
        live = live_out[block.addr]
        for insn in reversed(block.insns):
            reads = insn.mnemonic in V_READERS
            writes = insn.mnemonic in V_WRITERS
            if writes:
                if insn.mnemonic in OPTIMIZABLE and not live:
                    dead_lines.add(insn.line)
                live = reads
            elif reads:
                live = True
    return dead_lines


ADC_SEQ = [
    "ldh a, [nes_p]", "and $BF", "ld b, a", "ld a, d", "xor e", "cpl",
    "ld h, a", "ld a, d", "xor c", "and h", "and $80", "jr z, :+",
    "ld a, b", "or $40", "ld b, a", ":", "ld a, b", "ldh [nes_p], a",
    "ld a, c",
]
SBC_SEQ = [
    "ldh a, [nes_p]", "and $BF", "ld b, a", "ld a, d", "xor e",
    "ld h, a", "ld a, d", "xor c", "and h", "and $80", "jr z, :+",
    "ld a, b", "or $40", "ld b, a", ":", "ld a, b", "ldh [nes_p], a",
    "ld a, c",
]
BIT_SEQ = [
    "ldh a, [nes_p]", "and $BF", "bit 6, e", "jr z, :+", "or $40", ":",
    "ldh [nes_p], a",
]


def find_exact(lines: list[str], start: int, end: int, seq: list[str]) -> int | None:
    for i in range(start, end - len(seq) + 1):
        if [code(lines[i + k]) for k in range(len(seq))] == seq:
            return i
    return None


def remove_state_publish(lines: list[str], insn: Insn, end_i: int) -> bool:
    mnemonic = insn.mnemonic
    if mnemonic == "Adc":
        seq = ADC_SEQ
        remove = {0, 1, 2, 12, 14, 16, 17}
        marker = "inline exact 6502 ADC"
    elif mnemonic == "Sbc":
        seq = SBC_SEQ
        remove = {0, 1, 2, 11, 13, 15, 16}
        marker = "inline exact 6502 SBC"
    elif mnemonic == "Bit":
        seq = BIT_SEQ
        remove = {0, 1, 6}
        marker = "inline exact 6502 BIT"
    else:
        return False

    segment = "".join(lines[insn.line + 1:end_i])
    if marker not in segment:
        return False

    pos = find_exact(lines, insn.line + 1, end_i, seq)
    if pos is None:
        return False

    for k in sorted(remove):
        i = pos + k
        ind = indent_of(lines[i])
        if k == min(remove):
            lines[i] = f"{ind}; dead 6502 V state publication removed; host flags preserved\n"
        else:
            lines[i] = f"{ind}; dead V publication scaffold removed\n"
    return True


def optimize(lines: list[str]) -> tuple[int, int, int, int]:
    blocks = parse_blocks(lines)
    _, live_out = liveness(blocks)
    dead = dead_v_writers(blocks, live_out)

    adc = sbc = bit = unmatched = 0
    for block in blocks.values():
        for n, insn in enumerate(block.insns):
            if insn.line not in dead:
                continue
            end_i = block.insns[n + 1].line if n + 1 < len(block.insns) else block.end_i
            if not remove_state_publish(lines, insn, end_i):
                unmatched += 1
                continue
            if insn.mnemonic == "Adc":
                adc += 1
            elif insn.mnemonic == "Sbc":
                sbc += 1
            else:
                bit += 1

    return adc, sbc, bit, unmatched


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("asm", type=Path)
    args = p.parse_args()

    lines = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    adc, sbc, bit, unmatched = optimize(lines)
    args.asm.write_text("".join(lines), encoding="utf-8")
    print(
        f"lazy-v: skipped canonical overflow publication for {adc} ADC / {sbc} SBC / "
        f"{bit} BIT instruction(s); {unmatched} dead candidate(s) unmatched"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
