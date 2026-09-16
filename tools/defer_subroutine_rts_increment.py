#!/usr/bin/env python3
"""Defer multi-block guarded RTS PC increment to dynamic fallback.

`fast_subroutine_rts_dispatch.py` emits guards after the architectural 6502 RTS
`inc hl`, so every successful fast return pays that increment even though the
guard already knows its exact continuation.

This post-pass recognizes only those generated multi-block guard regions. For a
site whose selected continuations all have a non-zero low byte, compare the raw
stacked PC-1 instead by decrementing each low-byte compare. The high byte is then
unchanged, so the existing grouping remains valid. Move the single `inc hl` to
the unmatched fallback immediately before `jp nes_dispatch_hl`.

Sites containing a continuation at $xx00 are skipped conservatively because
PC-1 crosses a high-byte boundary there and would require regrouping guards.
The emulated stack pop, selected return set, guard order, direct jumps, and
fallback dispatcher are otherwise unchanged.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

MARKER = "guarded multi-block RTS return fast path"
BLOCK_ENTRY_RE = re.compile(r"^nes_[0-9A-Fa-f]{4}(?:_trace)?:$")
SOURCE_RE = re.compile(
    r"; \$[0-9A-Fa-f]{4}: \$[0-9A-Fa-f]{2} [A-Za-z0-9_]+ [A-Za-z0-9_]+"
)
CP_RE = re.compile(r"cp \$([0-9A-Fa-f]{2})$", re.IGNORECASE)
JP_TARGET_RE = re.compile(r"jp nes_([0-9A-Fa-f]{4})$", re.IGNORECASE)
HL_TARGET_RE = re.compile(r"ld hl, nes_([0-9A-Fa-f]{4})$", re.IGNORECASE)


def code(line: str) -> str:
    return line.split(";", 1)[0].strip()


def indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def next_code(lines: list[str], start: int, end: int) -> int | None:
    for i in range(start, min(end, len(lines))):
        if code(lines[i]):
            return i
    return None


def find_preceding_inc(lines: list[str], marker_i: int) -> int | None:
    floor = max(0, marker_i - 12)
    for i in range(marker_i - 1, floor - 1, -1):
        c = code(lines[i])
        if c == "inc hl":
            return i
        if SOURCE_RE.search(lines[i]) or c.startswith("SECTION ") or BLOCK_ENTRY_RE.fullmatch(c):
            break
        if c and not c.startswith(";"):
            # The generated fast path is expected to replace the JP immediately
            # after INC HL. Refuse to hop over another real instruction.
            break
    return None


def find_fallback(lines: list[str], marker_i: int) -> int | None:
    ceiling = min(len(lines), marker_i + 160)
    for i in range(marker_i + 1, ceiling):
        c = code(lines[i])
        if c == "jp nes_dispatch_hl":
            return i
        if SOURCE_RE.search(lines[i]) or c.startswith("SECTION ") or BLOCK_ENTRY_RE.fullmatch(c):
            break
    return None


def guard_target(lines: list[str], cp_i: int, fallback_i: int) -> int | None:
    """Return the exact continuation targeted by one low-byte guard."""
    targets: set[int] = set()
    for i in range(cp_i + 1, fallback_i):
        c = code(lines[i])
        # A low-guard miss label ends this guard arm.
        if c.startswith("nes_rts_sub_") and c.endswith("_next:"):
            break
        if m := JP_TARGET_RE.fullmatch(c):
            targets.add(int(m.group(1), 16))
        elif m := HL_TARGET_RE.fullmatch(c):
            targets.add(int(m.group(1), 16))
    if len(targets) != 1:
        return None
    return next(iter(targets))


def collect_low_guards(
    lines: list[str], marker_i: int, fallback_i: int
) -> list[tuple[int, int]] | None:
    """Return (cp-line, continuation) pairs for every low-byte guard."""
    guards: list[tuple[int, int]] = []
    i = marker_i + 1
    while i < fallback_i:
        if code(lines[i]) != "ld a, l":
            i += 1
            continue
        cp_i = next_code(lines, i + 1, min(fallback_i, i + 4))
        if cp_i is None:
            return None
        m = CP_RE.fullmatch(code(lines[cp_i]))
        if not m:
            return None
        target = guard_target(lines, cp_i, fallback_i)
        if target is None:
            return None
        if int(m.group(1), 16) != (target & 0xFF):
            return None
        guards.append((cp_i, target))
        i = cp_i + 1
    return guards if guards else None


def optimize(lines: list[str]) -> tuple[int, int, int, int]:
    deferred = 0
    guards_rewritten = 0
    page_boundary_skips = 0
    malformed_skips = 0

    marker_lines = [i for i, line in enumerate(lines) if MARKER in line]
    for marker_i in marker_lines:
        inc_i = find_preceding_inc(lines, marker_i)
        fallback_i = find_fallback(lines, marker_i)
        if inc_i is None or fallback_i is None:
            malformed_skips += 1
            continue

        guards = collect_low_guards(lines, marker_i, fallback_i)
        if not guards:
            malformed_skips += 1
            continue

        # Existing high-byte grouping remains valid only when PC-1 does not
        # borrow from the high byte.
        if any((target & 0xFF) == 0 for _cp_i, target in guards):
            page_boundary_skips += 1
            continue

        for cp_i, target in guards:
            ind = indent_of(lines[cp_i])
            stacked_low = ((target - 1) & 0xFF)
            lines[cp_i] = (
                f"{ind}cp ${stacked_low:02X} "
                f"; raw stacked RTS PC-1 for nes_{target:04X}\n"
            )
            guards_rewritten += 1

        ind = indent_of(lines[inc_i])
        lines[inc_i] = f"{ind}; RTS PC increment deferred to unmatched fallback\n"
        fallback_ind = indent_of(lines[fallback_i])
        lines[fallback_i] = (
            f"{fallback_ind}; unmatched multi-block RTS: apply architectural PC+1\n"
            f"{fallback_ind}inc hl\n"
            f"{fallback_ind}jp nes_dispatch_hl\n"
        )
        deferred += 1

    return deferred, guards_rewritten, page_boundary_skips, malformed_skips


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("asm", type=Path)
    args = p.parse_args()

    lines = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    deferred, guards, page_skips, malformed = optimize(lines)
    args.asm.write_text("".join(lines), encoding="utf-8")
    print(
        f"rts-sub-inc: deferred {deferred} multi-block RTS PC increment(s), "
        f"rewrote {guards} exact raw-stack guard(s); "
        f"skipped {page_skips} page-boundary / {malformed} unmatched site(s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
