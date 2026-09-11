#!/usr/bin/env python3
"""Specialize PPUSTATUS reads immediately consumed by `AND #$40`.

`inline_ppustatus_generated.py` preserves the complete $2002 return byte.  In a
very common sprite-0 polling idiom the next 6502 instruction immediately masks
that byte with $40, so bits 0-5 and 7 are never observable by translated code.
For those exact adjacent instruction pairs we synthesize only the sprite-0 bit
while preserving every $2002 side effect: vblank never reports sprite-0, the
virtual status bit 6 is updated, bit 7 is cleared, and the $2005/$2006 latch is
reset.  The following generated AND/flag/branch sequence is left untouched.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path


INSN_RE = re.compile(r"; \$[0-9A-Fa-f]{4}: \$[0-9A-Fa-f]{2} (\w+) (\w+)")


def code(line: str) -> str:
    return line.split(";", 1)[0].strip()


def is_insn_comment(line: str) -> bool:
    return INSN_RE.search(line.strip()) is not None


def indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def specialize(lines: list[str]) -> int:
    changed = 0
    i = 0
    while i < len(lines):
        if "inline exact PPUSTATUS ($2002) read" not in lines[i]:
            i += 1
            continue

        # This marker sits inside the generated code for the $2002-reading
        # instruction. Find the immediately following 6502 instruction.
        next_insn = i + 1
        while next_insn < len(lines) and not is_insn_comment(lines[next_insn]):
            next_insn += 1
        if next_insn >= len(lines):
            break

        m = INSN_RE.search(lines[next_insn].strip())
        if m is None or m.group(1) != "And" or m.group(2) != "Immediate":
            i = next_insn
            continue

        # Require that exact AND instruction to be #$40. Do not infer operand
        # values from the source comment because it intentionally omits them.
        after_and = next_insn + 1
        while after_and < len(lines) and not is_insn_comment(lines[after_and]):
            after_and += 1
        if not any(code(lines[j]) == "and $40" for j in range(next_insn + 1, after_and)):
            i = next_insn
            continue

        ind = indent_of(lines[i])
        tag = f"nes_sprite0_poll_{i}"
        no_hit = f"{tag}_no_hit"
        done = f"{tag}_done"

        # Keep low status bits exactly as the generic handler does because the
        # virtual status byte is persistent state even though the returned A is
        # immediately masked. Bit 6 is replaced by the synthesized hit result;
        # bit 7 is always cleared by the read side effect.
        replacement = [
            f"{ind}; specialized $2002 -> AND #$40 sprite-0 poll\n",
            f"{ind}ld a, [nes_ppu_status]\n",
            f"{ind}and $3F\n",
            f"{ind}ld e, a\n",
            f"{ind}ldh a, [rLY]\n",
            f"{ind}cp 144\n",
            f"{ind}jr nc, {no_hit}\n",
            f"{ind}ld b, a\n",
            f"{ind}ldh a, [nes_split_line]\n",
            f"{ind}ld c, a\n",
            f"{ind}ld a, b\n",
            f"{ind}cp c\n",
            f"{ind}jr c, {no_hit}\n",
            f"{ind}ld a, [nes_ppumask]\n",
            f"{ind}and $18\n",
            f"{ind}cp $18\n",
            f"{ind}jr nz, {no_hit}\n",
            f"{ind}ld a, [nes_oam_ram]\n",
            f"{ind}cp $EF\n",
            f"{ind}jr nc, {no_hit}\n",
            f"{ind}ld a, e\n",
            f"{ind}or $40\n",
            f"{ind}ld [nes_ppu_status], a\n",
            f"{ind}xor a\n",
            f"{ind}ld [nes_ppu_latch], a\n",
            f"{ind}ld a, $40\n",
            f"{ind}jr {done}\n",
            f"{no_hit}:\n",
            f"{ind}ld a, e\n",
            f"{ind}ld [nes_ppu_status], a\n",
            f"{ind}xor a\n",
            f"{ind}ld [nes_ppu_latch], a\n",
            f"{done}:\n",
        ]

        # Replace only the previously inlined status-handler body. Any stores
        # generated for the LDA itself remain in place, followed by the original
        # AND #$40 and its canonical 6502 flag publication.
        lines[i:next_insn] = replacement
        changed += 1
        i += len(replacement)

    return changed


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("asm", type=Path)
    args = p.parse_args()

    lines = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    changed = specialize(lines)
    args.asm.write_text("".join(lines), encoding="utf-8")
    print(f"sprite0-poll: specialized {changed} immediate $2002/AND #$40 sites")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
