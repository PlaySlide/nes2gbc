#!/usr/bin/env python3
"""Remove provably dead canonical 6502 Z/N shadow publications globally.

Earlier passes remove stores in a few branch-terminal shapes. This pass performs
instruction-level, inter-block liveness for the canonical Z and N flags and
removes any remaining `nes_z_shadow` / `nes_n_shadow` publication when every
path overwrites that flag before it can be observed.

Only the HRAM store is removed. The translated instruction and LR35902 host
flags are otherwise unchanged. JSR, PHP/BRK, translated-NMI poll entries,
dynamic exits, and unresolved control flow are conservative observation points.
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

ZN_WRITERS = {
    "Lda", "Ldx", "Ldy", "Tax", "Tay", "Txa", "Tya", "Tsx", "Pla",
    "And", "Ora", "Eor", "Adc", "Sbc", "Cmp", "Cpx", "Cpy", "Bit",
    "Inc", "Dec", "Inx", "Iny", "Dex", "Dey", "Asl", "Lsr", "Rol", "Ror",
    "Plp", "Rti",
}
OPTIMIZABLE_WRITERS = ZN_WRITERS - {"Plp", "Rti"}
Z_READERS = {"Beq", "Bne", "Php", "Brk", "Jsr"}
N_READERS = {"Bmi", "Bpl", "Php", "Brk", "Jsr"}
BRANCHES = {"Bcc", "Bcs", "Beq", "Bmi", "Bne", "Bpl", "Bvc", "Bvs"}
DYNAMIC_EXITS = {"Jsr", "Rts", "Brk"}


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


def remove_last_shadow_store(lines: list[str], start: int, end: int, shadow: str, flag: str) -> bool:
    forms = {f"ldh [{shadow}], a", f"ld [{shadow}], a"}
    for i in range(end - 1, start - 1, -1):
        if code(lines[i]) in forms:
            ind = indent_of(lines[i])
            lines[i] = f"{ind}; dead global {flag} shadow publication removed\n"
            return True
    return False


def optimize(lines: list[str]) -> tuple[int, int, int, int, int]:
    blocks = parse_blocks(lines)
    _, z_out = liveness(blocks, "z")
    _, n_out = liveness(blocks, "n")

    z_removed = 0
    n_removed = 0
    z_dead_defs = 0
    n_dead_defs = 0
    touched_insns: set[tuple[int, int]] = set()

    for block in blocks.values():
        live_z = z_out[block.addr]
        live_n = n_out[block.addr]

        for n in range(len(block.insns) - 1, -1, -1):
            insn = block.insns[n]
            z_after = live_z
            n_after = live_n

            if insn.mnemonic in OPTIMIZABLE_WRITERS:
                end_i = block.insns[n + 1].line if n + 1 < len(block.insns) else block.end_i
                if not z_after:
                    z_dead_defs += 1
                    if remove_last_shadow_store(lines, insn.line + 1, end_i, "nes_z_shadow", "Z"):
                        z_removed += 1
                        touched_insns.add((block.addr, insn.line))
                if not n_after:
                    n_dead_defs += 1
                    if remove_last_shadow_store(lines, insn.line + 1, end_i, "nes_n_shadow", "N"):
                        n_removed += 1
                        touched_insns.add((block.addr, insn.line))

            z_reads = insn.mnemonic in Z_READERS
            n_reads = insn.mnemonic in N_READERS
            writes = insn.mnemonic in ZN_WRITERS
            live_z = z_reads or (live_z and not writes)
            live_n = n_reads or (live_n and not writes)

    return len(touched_insns), z_removed, n_removed, z_dead_defs, n_dead_defs


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("asm", type=Path)
    args = p.parse_args()

    lines = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    touched, zr, nr, zd, nd = optimize(lines)
    args.asm.write_text("".join(lines), encoding="utf-8")
    print(
        f"dead-zn-global: removed {zr} Z + {nr} N shadow stores across "
        f"{touched} instruction(s); proved {zd}/{nd} dead Z/N definition(s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
