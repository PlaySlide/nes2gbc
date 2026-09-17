#!/usr/bin/env python3
"""Extend RTS reachability through strict inline-dispatch tail calls.

`specialize_inline_dispatchers.py` recognizes SMB-style JumpEngine calls whose
callee pops the JSR frame and tail-jumps to one of several state handlers.  The
ordinary multi-block RTS pass intentionally treats a JSR as returning to PC+3,
which is correct for normal subroutines but wrong for this strict dispatcher:
PC+3 is inline table data, and the selected handler eventually RTSes to the
*outer* caller's stack frame.

This wrapper keeps the existing guarded RTS machinery unchanged and only teaches
its caller-visible reachability walk about those already-proven inline-dispatch
fast paths.  For a terminal JSR carrying the strict-inline marker, the generated
case targets are treated as tail-call successors.  Exact popped-return guards,
code-size budgets, and generic-dispatch fallback remain unchanged.
"""

from __future__ import annotations

import re

import fast_subroutine_rts_dispatch as base


INLINE_MARKER = "strict inline-dispatch fast path for JSR"
INLINE_FALLBACK_RE = re.compile(
    r"^nes_inline_dispatch_[0-9A-Fa-f]{4}_fallback:$"
)

_original_successors = base.successors


def inline_dispatch_tail_targets(
    lines: list[str], block: base.Block, labels: set[int]
) -> set[int] | None:
    """Return proven tail-call targets for one specialized inline dispatcher.

    None means this is not an inline-dispatch JSR and normal JSR semantics should
    be used.  An empty set means the marker was present but no canonical case
    target survived, so conservatively stop the caller-visible walk there.
    """
    if not block.insns or block.insns[-1][2] != "Jsr":
        return None

    start = block.insns[-1][0] + 1
    saw_marker = False
    found: set[int] = set()

    for j in range(start, block.end_i):
        raw = lines[j]
        c = base.code(raw)

        if INLINE_MARKER in raw:
            saw_marker = True
            continue
        if not saw_marker:
            continue

        # Everything after this label is the untouched slow fallback for the
        # original JSR.  Only the proven direct cases above it are tail edges.
        if INLINE_FALLBACK_RE.fullmatch(c):
            break

        # Case arms contain both BANK(nes_XXXX) and LD HL,nes_XXXX references;
        # a set naturally deduplicates them.  Dispatcher-local labels do not
        # match TARGET_RE because they are not canonical `nes_XXXX` names.
        for m in base.TARGET_RE.finditer(c):
            target = int(m.group(1), 16)
            if target in labels:
                found.add(target)

    return found if saw_marker else None


def successors(
    lines: list[str], block: base.Block, labels: set[int]
) -> set[int]:
    if block.insns and block.insns[-1][2] == "Jsr":
        tails = inline_dispatch_tail_targets(lines, block, labels)
        if tails is not None:
            return tails
    return _original_successors(lines, block, labels)


# reachable_rts() resolves `successors` through the module global at runtime, so
# patch only that one relation and reuse the existing parser/ranker/emitter.
base.successors = successors


if __name__ == "__main__":
    raise SystemExit(base.main())
