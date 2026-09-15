#!/usr/bin/env python3
"""Make emitter-proven A-resident edges dirty in release builds.

The Rust emitter creates private ``nes_XXXX_fast_a`` entries only when a
same-bank static predecessor arrives with host A equal to 6502 A and the target
can consume that value without its canonical HRAM reload.  The first residency
stage deliberately left ``nes_a`` canonical at the source.

This late pass removes the other half of that round-trip: for a private edge,
it defers the source's final ``ldh [nes_a], a`` in release builds when the
shared fast target republishes ``nes_a`` before any helper, control transfer,
or canonical-state reread.  PROFILE_TRACE keeps the source spill because those
builds intentionally take the canonical target entry.

Running late is intentional.  All existing generated-ASM liveness/cache passes
continue to see canonical state and therefore cannot infer extra dead state from
this optimization (the failure mode that previously broke SMB's mushroom
transition).
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

CANON_RE = re.compile(r"^nes_([0-9A-Fa-f]{4}):$")
FAST_RE = re.compile(r"^nes_([0-9A-Fa-f]{4})_fast_a:$")
FAST_JP_RE = re.compile(r"^jp nes_([0-9A-Fa-f]{4})_fast_a$")
STORE_A = "ldh [nes_a], a"
LOAD_A = "ldh a, [nes_a]"
PROFILE_IF = "IF DEF(NES2GBC_PROFILE_TRACE)"


def code(line: str) -> str:
    return line.split(";", 1)[0].strip()


def target_republishes_before_observer(lines: list[str], fast_i: int) -> bool:
    """The dirty value must become canonical before anything can observe HRAM A."""
    for i in range(fast_i + 1, len(lines)):
        c = code(lines[i])
        if not c:
            continue
        if c.startswith("SECTION ") or CANON_RE.fullmatch(c) or FAST_RE.fullmatch(c):
            return False
        if c == STORE_A:
            return True
        if "[nes_a]" in c:
            return False
        low = c.lower()
        if low.startswith(("call ", "jp ", "jr ", "ret", "reti", "rst ")):
            return False
        if c.startswith("PROFILE_INC "):
            return False
    return False


def suffix_preserves_host_a(lines: list[str], start: int, end: int) -> bool:
    """After the deferred spill, allow only instructions that cannot change A."""
    for i in range(start, end):
        c = code(lines[i])
        if not c:
            continue
        if c.endswith(":"):
            continue
        low = c.lower()
        # Stores preserve A.  This covers the usual Z/N shadow publication that
        # follows the final nes_a commit as well as direct RAM publication.
        if re.fullmatch(r"ldh? \[[^\]]+\], a", low):
            continue
        if low == "nop":
            continue
        return False
    return True


def find_profile_transfer_start(lines: list[str], fast_jp_i: int, block_start: int) -> int | None:
    # emit_fast_a_target has a tiny fixed conditional wrapper.  Find its IF
    # rather than relying on an exact line distance so comments remain harmless.
    for i in range(fast_jp_i - 1, max(block_start, fast_jp_i - 10) - 1, -1):
        c = code(lines[i])
        if c == PROFILE_IF:
            return i
        if c.startswith("SECTION ") or CANON_RE.fullmatch(c):
            break
    return None


def optimize(lines: list[str]) -> tuple[int, int, int]:
    fast_labels: dict[int, int] = {}
    for i, line in enumerate(lines):
        m = FAST_RE.fullmatch(code(line))
        if m:
            fast_labels[int(m.group(1), 16)] = i

    safe_targets = {
        addr
        for addr, i in fast_labels.items()
        if target_republishes_before_observer(lines, i)
    }

    block_start = 0
    candidates = 0
    edits: list[int] = []
    for i, line in enumerate(lines):
        c = code(line)
        if CANON_RE.fullmatch(c):
            block_start = i
            continue
        m = FAST_JP_RE.fullmatch(c)
        if not m:
            continue
        candidates += 1
        target = int(m.group(1), 16)
        if target not in safe_targets:
            continue

        transfer_i = find_profile_transfer_start(lines, i, block_start)
        if transfer_i is None:
            continue

        store_i = None
        for j in range(transfer_i - 1, block_start, -1):
            cj = code(lines[j])
            if cj == STORE_A:
                store_i = j
                break
            low = cj.lower()
            if low.startswith(("call ", "jp ", "jr ", "ret", "reti", "rst ")):
                break
        if store_i is None:
            continue
        if not suffix_preserves_host_a(lines, store_i + 1, transfer_i):
            continue
        edits.append(store_i)

    for store_i in sorted(set(edits), reverse=True):
        indent = lines[store_i][: len(lines[store_i]) - len(lines[store_i].lstrip())]
        lines[store_i:store_i + 1] = [
            f"{indent}; A-residency: canonical spill needed only by PROFILE_TRACE\n",
            f"{indent}{PROFILE_IF}\n",
            f"{indent}{STORE_A}\n",
            f"{indent}ENDC\n",
        ]

    return candidates, len(safe_targets), len(set(edits))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("asm", type=Path)
    args = p.parse_args()

    lines = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    candidates, safe_targets, removed = optimize(lines)
    args.asm.write_text("".join(lines), encoding="utf-8")
    print(
        f"a-dirty: deferred {removed}/{candidates} source A spill(s); "
        f"{safe_targets} private target(s) repay canonical nes_a before observers"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
