#!/usr/bin/env python3
"""Use an 8-bit translated-code bank switch helper for statically known targets.

All translated code banks are allocated below 256, and execution reaches generated
translated code through the normal reset dispatcher, which explicitly clears the
MBC5 high bank bit at $3000. Generated static transfers therefore do not need to
rewrite that high bit on every cross-bank edge.

Keep XOR A in the helper: it preserves the same LR35902 host-flag normalization
as nes_jump_known_hl_a. The only semantic change is omitting the redundant
`ld [$3000],a` MBC5 write.

Only exact generated patterns are rewritten:

    ld a, $NN            or ld a, BANK(nes_XXXX)
    ld hl, nes_XXXX
    jp nes_jump_known_hl_a

Anything unfamiliar is left untouched.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

TARGET_RE = re.compile(r"ld hl, nes_([0-9A-Fa-f]{4})$")
BANK_IMM_RE = re.compile(r"ld a, \$([0-9A-Fa-f]{2})$")
BANK_LABEL_RE = re.compile(r"ld a, BANK\(nes_([0-9A-Fa-f]{4})\)$")
HELPER = "nes_jump_known_hl_a_8bit"


def code(line: str) -> str:
    return line.split(";", 1)[0].strip()


def prev_code(lines: list[str], start: int) -> int | None:
    i = start
    while i >= 0:
        if code(lines[i]):
            return i
        i -= 1
    return None


def optimize(lines: list[str]) -> int:
    changed = 0
    for i, line in enumerate(lines):
        if code(line) != "jp nes_jump_known_hl_a":
            continue

        hl_i = prev_code(lines, i - 1)
        if hl_i is None:
            continue
        tm = TARGET_RE.fullmatch(code(lines[hl_i]))
        if not tm:
            continue

        bank_i = prev_code(lines, hl_i - 1)
        if bank_i is None:
            continue
        bank_code = code(lines[bank_i])
        bm = BANK_IMM_RE.fullmatch(bank_code)
        bl = BANK_LABEL_RE.fullmatch(bank_code)
        if not bm and not bl:
            continue

        # BANK(nes_XXXX) must refer to the same target loaded into HL.
        if bl and bl.group(1).lower() != tm.group(1).lower():
            continue

        ind = line[: len(line) - len(line.lstrip())]
        lines[i] = f"{ind}jp {HELPER} ; 8-bit translated-code bank switch\n"
        changed += 1

    return changed


def helper() -> str:
    return (
        "\nSECTION \"Fast 8-bit translated bank switch\", ROM0\n"
        f"{HELPER}:\n"
        "    ; A = translated code bank (<256), HL = linked ROMX target.\n"
        "    ; MBC5 bank bit 8 is already zero from the initial dispatcher.\n"
        "    ld [nes_current_code_bank], a\n"
        "    ld [$2000], a\n"
        "    xor a ; preserve host-flag normalization of the original helper\n"
        "    jp hl\n"
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("asm", type=Path)
    args = p.parse_args()

    lines = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    changed = optimize(lines)
    if changed and not any(code(line) == f"{HELPER}:" for line in lines):
        lines.append(helper())
    args.asm.write_text("".join(lines), encoding="utf-8")
    print(
        f"bank8-fast: rewrote {changed} known cross-bank transfer(s); "
        "kept XOR flag normalization, skipped redundant MBC5 high-bit write"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
