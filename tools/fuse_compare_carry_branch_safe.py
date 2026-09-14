#!/usr/bin/env python3
"""Fuse exact terminal CMP/CPX/CPY -> BCC/BCS without removing canonical C.

The earlier carry-branch experiment also performed inter-block dead-C-shadow
elision and regressed collision logic in Donkey Kong / Balloon Fight.  This
version deliberately does *not* perform any liveness optimization.

For an adjacent compare/carry-branch pair, the folded compare already has the
exact 6502 carry in the LR35902 carry flag immediately after CCF.  Materialize
the canonical nes_c_shadow with JR/INC (which preserve host C), keep that store
unconditionally, then let the terminal branch consume host C directly instead
of reloading/testing nes_c_shadow.

All architectural 6502 state remains published exactly as before.  No CFG,
stack, interrupt, branch target, or safe-point behavior is changed.
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
COMPARES = {"Cmp", "Cpx", "Cpy"}


def code(line: str) -> str:
    return line.split(";", 1)[0].strip()


@dataclass
class Insn:
    line: int
    pc: int
    mnemonic: str


@dataclass
class Block:
    start: int
    end: int
    insns: list[Insn]


def parse_blocks(lines: list[str]) -> list[Block]:
    labels: list[int] = []
    for i, line in enumerate(lines):
        if BLOCK_RE.fullmatch(code(line)):
            labels.append(i)

    out: list[Block] = []
    for n, start in enumerate(labels):
        raw_end = labels[n + 1] if n + 1 < len(labels) else len(lines)
        end = raw_end
        for j in range(start + 1, raw_end):
            if code(lines[j]).startswith("SECTION "):
                end = j
                break
        insns: list[Insn] = []
        for j in range(start + 1, end):
            m = INSN_RE.search(lines[j])
            if m:
                insns.append(Insn(j, int(m.group(1), 16), m.group(3)))
        out.append(Block(start, end, insns))
    return out


def next_code(lines: list[str], start: int, end: int) -> int | None:
    i = start
    while i < end:
        if code(lines[i]):
            return i
        i += 1
    return None


def optimize(lines: list[str]) -> int:
    fused = 0

    for block in parse_blocks(lines):
        if len(block.insns) < 2:
            continue
        producer = block.insns[-2]
        branch = block.insns[-1]
        if producer.mnemonic not in COMPARES or branch.mnemonic not in {"Bcc", "Bcs"}:
            continue

        # Exact tail emitted by shrink_compare_generated.py:
        #   ccf
        #   ld a,$00
        #   rl a
        #   ldh [nes_c_shadow],a
        #   ld a,c
        #   ldh [nes_z_shadow],a
        #   ldh [nes_n_shadow],a
        # Replace only RL A with a carry-preserving 0/1 materialization.
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

        # Require the canonical branch reload/test shape.  Anything unfamiliar
        # is left untouched rather than inferred.
        load_i = next_code(lines, branch.line + 1, block.end)
        if load_i is None or code(lines[load_i]) != "ldh a, [nes_c_shadow]":
            continue
        and_i = next_code(lines, load_i + 1, block.end)
        if and_i is None or code(lines[and_i]) != "and a":
            continue
        skip_i = next_code(lines, and_i + 1, block.end)
        expected = "jr nz, :+" if branch.mnemonic == "Bcc" else "jr z, :+"
        if skip_i is None or code(lines[skip_i]) != expected:
            continue

        ind = lines[rl_i][: len(lines[rl_i]) - len(lines[rl_i].lstrip())]
        label = f".cmpcarry_safe_{producer.pc:04X}_zero"
        lines[rl_i] = (
            f"{ind}; preserve host C while publishing exact 6502 C shadow\n"
            f"{ind}jr nc, {label}\n"
            f"{ind}inc a\n"
            f"{label}:\n"
        )

        bind = lines[load_i][: len(lines[load_i]) - len(lines[load_i].lstrip())]
        lines[load_i] = f"{bind}; safe fused carry branch: canonical C remains published\n"
        lines[and_i] = f"{bind}; carry shadow reload/test removed\n"
        direct_skip = "c" if branch.mnemonic == "Bcc" else "nc"
        lines[skip_i] = f"{bind}jr {direct_skip}, :+\n"
        fused += 1

    return fused


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("asm", type=Path)
    args = p.parse_args()

    lines = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    fused = optimize(lines)
    args.asm.write_text("".join(lines), encoding="utf-8")
    print(
        f"carry-branch-safe: fused {fused} terminal CMP/CPX/CPY -> BCC/BCS pairs; "
        "kept every C shadow store"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
