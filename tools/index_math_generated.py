#!/usr/bin/env python3
"""Conservative indexed-address peepholes for generated LR35902 code."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


def code(line: str) -> str:
    return line.split(";", 1)[0].strip()


def indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


SOURCE_RE = re.compile(
    r"; \$[0-9A-Fa-f]{4}: \$[0-9A-Fa-f]{2} ([A-Za-z0-9_]+) ([A-Za-z0-9_]+)"
)
ABS_RAM_BASE_RE = re.compile(r"ld hl, \$(C[0-7][0-9A-Fa-f]{2})$")
INDEX_LOADS = {"ldh a, [nes_x]", "ldh a, [nes_y]"}
RMW_MNEMONICS = {"Inc", "Dec", "Asl", "Lsr", "Rol", "Ror"}


def is_absolute_indexed_rmw_source(lines: list[str], start: int) -> bool:
    """Require the nearest source marker to be one AbsoluteX/Y RMW opcode."""
    for i in range(start - 1, max(-1, start - 20), -1):
        m = SOURCE_RE.search(lines[i])
        if m:
            return m.group(1) in RMW_MNEMONICS and m.group(2) in {
                "AbsoluteX",
                "AbsoluteY",
            }
        c = code(lines[i])
        if c.startswith("SECTION ") or (c.startswith("nes_") and c.endswith(":")):
            break
    return False


def body_preserves_hl(lines: list[str], start: int, end: int) -> bool:
    """Conservatively prove the RMW body leaves the effective address in HL."""
    reg_re = re.compile(r"\b(?:hl|h|l)\b", re.IGNORECASE)
    for line in lines[start:end]:
        if SOURCE_RE.search(line):
            return False
        c = code(line)
        if not c:
            continue
        low = c.lower()
        if low.endswith(":") or low.startswith(("call ", "jp ", "jr ", "ret", "reti")):
            return False
        if reg_re.search(low):
            return False
    return True


def fuse_absolute_indexed_rmw_recalc(lines: list[str]) -> int:
    """Reuse first HL for safe AbsoluteX/Y internal-RAM RMW write-back."""
    fused = 0
    i = 0
    first_tail = [
        "add l",
        "ld l, a",
        "jr nc, :+",
        "inc h",
        ":",
        "ld a, [hl]",
    ]

    while i + 18 < len(lines):
        bm = ABS_RAM_BASE_RE.fullmatch(code(lines[i]))
        if not bm or code(lines[i + 1]) not in INDEX_LOADS:
            i += 1
            continue
        if [code(lines[i + 2 + n]) for n in range(len(first_tail))] != first_tail:
            i += 1
            continue
        if not is_absolute_indexed_rmw_source(lines, i):
            i += 1
            continue

        push_i = None
        ceiling = min(len(lines) - 10, i + 48)
        for j in range(i + 8, ceiling):
            if SOURCE_RE.search(lines[j]):
                break
            if code(lines[j]) == "push af":
                push_i = j
                break
        if push_i is None or not body_preserves_hl(lines, i + 8, push_i):
            i += 1
            continue

        expected_second = [
            code(lines[i]),
            code(lines[i + 1]),
            "add l",
            "ld l, a",
            "jr nc, :+",
            "inc h",
            ":",
            "pop af",
            "ld [hl], a",
        ]
        actual_second = [code(lines[push_i + 1 + n]) for n in range(9)]
        if actual_second != expected_second:
            i += 1
            continue

        ind = indent_of(lines[push_i])
        lines[push_i] = f"{ind}; reused first absolute-indexed RMW effective address in HL\n"
        # Delete only PUSH AF through POP AF and the duplicate address setup.
        # Keep the final LD [HL],A at push_i+9 exactly unchanged.
        for j in range(push_i + 1, push_i + 9):
            lines[j] = f"{ind}; redundant absolute-indexed RMW address recompute removed\n"
        fused += 1
        i = push_i + 10

    return fused


def fuse_recent_index_reload(lines: list[str]) -> int:
    """Drop an X/Y reload when A still equals the just-published X/Y value."""
    fused = 0
    for i in range(len(lines)):
        load = code(lines[i])
        if load not in {"ldh a, [nes_x]", "ldh a, [nes_y]"}:
            continue
        target = load.replace("ldh a, [", "ldh [").replace("]", "], a")
        k = i - 1
        while k >= 0 and code(lines[k]) in {
            "",
            "ldh [nes_z_shadow], a",
            "ldh [nes_n_shadow], a",
        }:
            k -= 1
        if k < 0 or code(lines[k]) != target:
            continue
        ind = indent_of(lines[i])
        lines[i] = f"{ind}; fused index reload: A still holds {target[5:10]}\n"
        fused += 1
    return fused


def trim_page_aligned_absolute_index(lines: list[str]) -> int:
    """For base $xx00 + 8-bit index, carry into H is impossible."""
    trimmed = 0
    base_re = re.compile(r"ld hl, \$([0-9A-Fa-f]{2})00")
    for i in range(len(lines) - 6):
        bm = base_re.fullmatch(code(lines[i]))
        if not bm:
            continue
        if code(lines[i + 1]) not in {"ldh a, [nes_x]", "ldh a, [nes_y]"}:
            continue
        expected = ["add l", "ld l, a", "jr nc, :+", "inc h", ":"]
        if [code(lines[i + 2 + n]) for n in range(5)] != expected:
            continue

        ind = indent_of(lines[i])
        # Keep the original LD HL so address-space selection is unchanged.
        # With L=$00, copying the 8-bit index into L is exactly base+index.
        # AND A reproduces the externally visible C=0/Z(index) state of ADD L;
        # H is irrelevant to NES status semantics but this avoids leaving stale
        # GB carry around for later peepholes.
        lines[i + 2] = f"{ind}ld l, a\n"
        lines[i + 3] = f"{ind}and a ; page-aligned absolute index, no carry\n"
        lines[i + 4] = f"{ind}; carry branch removed\n"
        lines[i + 5] = f"{ind}; high-byte increment impossible\n"
        lines[i + 6] = f"{ind}; page-aligned index label removed\n"
        trimmed += 1
    return trimmed


def trim_zero_page_zero_base(lines: list[str]) -> int:
    """`zp=$00 + X/Y` does not need an 8-bit immediate ADD."""
    trimmed = 0
    for i in range(len(lines) - 1):
        if code(lines[i]) not in {"ldh a, [nes_x]", "ldh a, [nes_y]"}:
            continue
        if code(lines[i + 1]) != "add $00":
            continue
        ind = indent_of(lines[i + 1])
        lines[i + 1] = f"{ind}and a ; zero-page base $00\n"
        trimmed += 1
    return trimmed


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("asm", type=Path)
    args = p.parse_args()
    lines = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)

    rmw = fuse_absolute_indexed_rmw_recalc(lines)
    reloads = fuse_recent_index_reload(lines)
    pages = trim_page_aligned_absolute_index(lines)
    zp0 = trim_zero_page_zero_base(lines)

    args.asm.write_text("".join(lines), encoding="utf-8")
    print(
        f"index-math: fused {reloads} immediate X/Y reloads, "
        f"reused {rmw} absolute-indexed RMW address(es), "
        f"trimmed {pages} page-aligned absolute indexes, {zp0} zero-page-$00 adds"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
